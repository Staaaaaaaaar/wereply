import tempfile
import json
from unittest.mock import patch
import unittest
from pathlib import Path

from wereply.core import Engine, Store, generate


def message(n, **kw):
    return dict(message_id=str(n), account_id='a', chat_id='wxid_friend',
                sender_id='wxid_friend', is_self=False, timestamp=100,
                type=1, text='你好', **kw)


def page(*messages, cursor='c', more=False, account='a'):
    return dict(account_id=account, messages=list(messages), next_cursor=cursor, has_more=more)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / 'state.db')
        with self.s.db:
            self.s.db.execute("INSERT INTO rules VALUES ('wxid_friend','fixed','收到',1)")

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def ready(self):
        self.s.ingest(page(message(0)))
        with self.s.db:
            self.s.put('active', True)

    def jobs(self):
        return self.s.db.execute('SELECT * FROM jobs').fetchall()

    def test_baseline_spans_pages(self):
        self.s.ingest(page(message(0), more=True))
        self.assertFalse(self.s.get('initialized', False))
        self.s.ingest(page(message(1)))
        self.assertTrue(self.s.get('initialized'))
        self.assertEqual(len(self.jobs()), 0)

    def test_replay_same_second_and_resume(self):
        self.ready()
        p=page(message(1), message(2), cursor='next')
        self.s.ingest(p)
        self.s.ingest(p)
        self.assertEqual(len(self.jobs()), 2)
        other=Store(Path(self.tmp.name) / 'state.db')
        self.assertEqual(other.get('cursor'), 'next')
        other.db.close()

    def test_atomic_account_rejection(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.s.ingest(page(message(1), dict(message(2), account_id='bad'), cursor='bad'))
        self.assertEqual(self.s.get('cursor'), 'c')
        self.assertEqual(len(self.jobs()), 0)

    def test_ignore_self_unknown_groups_and_media(self):
        self.ready()
        for n, change in enumerate([{'is_self':True}, {'is_self':None},
                                   {'chat_id':'a@chatroom'}, {'type':3}], 1):
            self.s.ingest(page(dict(message(n), **change)))
        self.assertEqual(len(self.jobs()), 0)

    def test_paused_messages_not_queued(self):
        self.s.ingest(page())
        self.s.ingest(page(message(1)))
        self.assertEqual(len(self.jobs()), 0)

    def test_send_persisted_uuid_and_unknown_not_retried(self):
        self.ready()
        calls=[]
        class Client:
            def call(_, *args, **kw):
                calls.append((args,kw))
                if args[0]=='doctor':
                    return dict(account_id='a',read_available=True,send_available=True,account_matches=True)
                if args[0]=='new-messages': return page(message(1))
                if args[0]=='send':
                    job=self.jobs()[0]
                    self.assertEqual(job['status'], 'sending')
                    self.assertIn(job['id'], args)
                    raise TimeoutError()
        e=Engine(self.s,Client());e.tick();e.tick()
        self.assertEqual(self.jobs()[0]['status'],'unknown')
        self.assertEqual(sum(a[0]=='send' for a,k in calls),1)

    def test_no_send_when_unavailable(self):
        self.ready()
        class Client:
            def call(_, *args, **kw):
                if args[0]=='doctor':
                    return dict(account_id='a',read_available=True,send_available=False,account_matches=True)
                if args[0]=='new-messages': return page(message(1))
                raise AssertionError('must not send')
        Engine(self.s,Client()).tick()
        self.assertEqual(self.jobs()[0]['status'],'pending')

    def test_llm_context_isolated_and_bounded_at_trigger(self):
        self.ready()
        self.s.ingest(page(message(1)))
        job=self.jobs()[0]
        self.s.ingest(page(dict(message(2), chat_id='other', text='private'),
                           dict(message(3), text='future')))
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _):
                return json.dumps({'choices':[{'message':{'content':'回复'}}]}).encode()
        def request(req, timeout):
            data=json.loads(req.data)
            content=str(data['messages'])
            self.assertNotIn('private', content)
            self.assertNotIn('future', content)
            self.assertEqual(data['messages'][-1]['content'], '你好')
            return Response()
        with patch.dict('os.environ', {'WEREPLY_LLM_URL':'https://example.test/chat',
                                     'WEREPLY_LLM_MODEL':'test'}), patch('urllib.request.urlopen', request):
            self.assertEqual(generate(self.s.db, job), '回复')

    def test_baseline_large_page_and_immediate_catchup_signal(self):
        owner = self
        class Client:
            def call(_, *args, **kw):
                if args[0]=='doctor':
                    return dict(account_id='a',read_available=True,send_available=False,account_matches=True)
                owner.assertEqual(args[args.index('--limit')+1], '5000')
                return page(message(1), message(2), cursor='next', more=True)
        self.assertTrue(Engine(self.s,Client()).tick())
        self.assertEqual(self.s.snapshot()['baseline_messages'], 2)
        self.s.ingest(page(message(2), cursor='last'))
        self.assertEqual(self.s.snapshot()['baseline_messages'], 2)
        self.assertEqual(len(self.jobs()), 0)
        self.assertTrue(self.s.get('initialized'))

    def test_repeated_cursor_stops_continuous_paging(self):
        self.s.ingest(page(cursor='same', more=True))
        class Client:
            def call(_, *args, **kw):
                if args[0]=='doctor':
                    return dict(account_id='a',read_available=True,send_available=False,account_matches=True)
                return page(cursor='same', more=True)
        with self.assertRaisesRegex(ValueError, '游标未推进'):
            Engine(self.s,Client()).tick()
        self.assertFalse(self.s.get('initialized', False))

    def test_claim_rechecks_rule_after_generation(self):
        self.ready(); self.s.ingest(page(message(1)))
        job=self.jobs()[0]
        self.s.save_rule('wxid_friend','fixed','新回复',True,'朋友')
        self.assertFalse(self.s.claim(job,'旧回复'))
        self.assertEqual(self.jobs()[0]['status'],'cancelled')

    def test_claim_rechecks_pause_and_cancelled_status(self):
        self.ready(); self.s.ingest(page(message(1)))
        job=self.jobs()[0]
        with self.s.db:
            self.s.put('active',False)
        self.assertFalse(self.s.claim(job,'旧回复'))
        with self.s.db:
            self.s.put('active',True)
        self.assertFalse(self.s.claim(job,'旧回复'))

    def test_pause_during_llm_generation_never_sends(self):
        self.ready()
        self.s.save_rule('wxid_friend','llm','prompt',True,'朋友')
        self.s.ingest(page(message(1)))
        class Client:
            def call(_, *args, **kw):
                if args[0]=='doctor':
                    return dict(account_id='a',read_available=True,send_available=True,account_matches=True)
                if args[0]=='new-messages': return page()
                raise AssertionError('must not send')
        def slow_generate(db, job):
            with self.s.db:
                self.s.put('active', False)
            return 'reply'
        with patch('wereply.core.generate', slow_generate):
            Engine(self.s,Client()).tick()
        self.assertEqual(self.jobs()[0]['status'],'cancelled')

    def test_ingest_reports_unmatched_and_queued(self):
        self.ready()
        self.s.ingest(page(message(1), dict(message(2),chat_id='wxid_unknown')))
        batch=self.s.snapshot()['last_batch']
        self.assertEqual(batch['reasons']['queued'],1)
        self.assertEqual(batch['reasons']['unmatched'],1)

    def test_opening_store_does_not_modify_inflight(self):
        self.ready();self.s.ingest(page(message(1)))
        self.s.update_job(self.jobs()[0]['id'],'sending')
        other=Store(Path(self.tmp.name)/'state.db')
        self.assertEqual(other.snapshot()['jobs'][0]['status'],'sending')
        other.db.close()


if __name__=='__main__':
    unittest.main()
