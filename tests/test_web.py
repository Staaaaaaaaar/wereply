import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class WebTests(unittest.TestCase):
    def test_local_console_rules_pause_and_origin_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'fake.py'
            fake.write_text('''import json,sys
if sys.argv[1]=='doctor':
 print(json.dumps(dict(account_id='a', read_available=True, send_available=False, account_matches=True)))
elif sys.argv[1]=='contacts':
 if '--detail' in sys.argv:
  print(json.dumps(dict(username='wxid_friend',nick_name='测试联系人',remark='测试备注')))
 else:
  print(json.dumps([dict(username='wxid_friend',nick_name='测试联系人',remark='测试备注')]))
else:
 print(json.dumps(dict(account_id='a', messages=[], next_cursor='c', has_more=False)))
''')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            root = f'http://127.0.0.1:{port}'
            proc = subprocess.Popen([sys.executable, '-m', 'wereply.app', '--port', str(port),
                '--db', str(Path(tmp)/'state.db'), '--interval', '1',
                '--wecli', f'{sys.executable} {fake}'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                state = None
                for _ in range(60):
                    if proc.poll() is not None:
                        self.fail(proc.communicate()[1].decode())
                    try:
                        with urllib.request.urlopen(root+'/api/state', timeout=1) as r:
                            state=json.load(r)
                        if state['initialized']:
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    time.sleep(.05)
                self.assertTrue(state and state['initialized'])
                with urllib.request.urlopen(root) as r:
                    self.assertIn('回复控制台', r.read().decode())
                def post(path, data, token=True):
                    headers={'Content-Type':'application/json'}
                    if token:
                        headers['X-Wereply-Token']=state['token']
                    return urllib.request.urlopen(urllib.request.Request(root+path,
                        data=json.dumps(data).encode(), headers=headers), timeout=2)
                with self.assertRaises(urllib.error.HTTPError) as err:
                    post('/api/active', {'active':True}, False)
                self.assertEqual(err.exception.code,403)
                with post('/api/contacts', {'query':'测试'}) as r:
                    self.assertEqual(json.load(r)['contacts'][0]['username'], 'wxid_friend')
                with self.assertRaises(urllib.error.HTTPError) as err:
                    post('/api/rules', dict(chat='wrong_alias',mode='fixed',body='收到',enabled=True))
                self.assertEqual(err.exception.code,400)
                with post('/api/rules', dict(chat='wxid_friend',mode='fixed',body='收到',enabled=True)) as r:
                    self.assertEqual(r.status,200)
                with post('/api/active', {'active':True}):
                    pass
                with urllib.request.urlopen(root+'/api/state') as r:
                    snapshot=json.load(r)
                self.assertTrue(snapshot['active'])
                self.assertEqual(snapshot['rules'][0]['body'],'收到')
                self.assertEqual(snapshot['rules'][0]['name'],'测试备注')
                self.assertIn('job_counts',snapshot)
                with post('/api/active', {'active':False}):
                    pass
                with post('/api/rules/delete', {'chat':'wxid_friend'}):
                    pass
                with urllib.request.urlopen(root+'/api/state') as r:
                    self.assertEqual(json.load(r)['rules'], [])
            finally:
                proc.terminate()
                proc.communicate(timeout=5)


if __name__=='__main__':
    unittest.main()
