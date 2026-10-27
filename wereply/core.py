import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path


class Wecli:
    def __init__(self, command, config=None):
        self.command = shlex.split(command)
        if not self.command:
            raise ValueError('wecli 命令不能为空')
        self.command[0] = os.path.expanduser(self.command[0])
        if len(self.command) == 1 and re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(self.command[0]).name):
            self.command += ['-m', 'wecli']
        if config:
            self.command += ["--config", config]

    def call(self, *args, text=None, allow_failure=False):
        try:
            result = subprocess.run(self.command + list(args), input=text, text=True,
                                    encoding='utf-8', errors='replace',
                                    capture_output=True, timeout=120)
        except FileNotFoundError:
            raise RuntimeError(f'找不到 wecli 命令：{self.command[0]}；请检查 --wecli 路径') from None
        except subprocess.TimeoutExpired:
            raise RuntimeError('wecli 调用超过 120 秒；发送操作需先核对结果，不要直接重发') from None
        diagnostic = ' '.join(result.stderr.split())[-320:]
        suffix = f'：{diagnostic}' if diagnostic else '；stderr 为空，请检查 --wecli 是否指向正确命令'
        try:
            data = json.loads(result.stdout)
        except ValueError:
            # Never display stdout here: it may contain private message records.
            raise RuntimeError(f"wecli 未返回有效 JSON（退出码 {result.returncode}）{suffix}") from None
        if not isinstance(data, dict) and not isinstance(data, list):
            raise RuntimeError("wecli 返回格式错误")
        if result.returncode and not allow_failure:
            raise RuntimeError(f"wecli 调用失败（退出码 {result.returncode}）{suffix}")
        return data

    def contacts(self, query):
        result = self.call('contacts', '--query', query, '--limit', '30', '--format', 'json')
        if not isinstance(result, list):
            raise ValueError('联系人列表格式不正确')
        return [dict(username=c['username'], nick_name=c.get('nick_name', ''),
                     remark=c.get('remark', '')) for c in result
                if isinstance(c.get('username'), str) and not c['username'].endswith('@chatroom')
                and not c['username'].startswith('gh_')]

    def contact(self, chat):
        try:
            result = self.call('contacts', '--detail', chat, '--format', 'json')
        except RuntimeError as exc:
            raise ValueError('无法核对联系人，请按昵称或备注搜索并选择对象') from exc
        if (not isinstance(result, dict) or result.get('username') != chat
                or result.get('is_group') or result.get('is_subscription')):
            raise ValueError('请搜索并选择私聊联系人；微信号、昵称不能直接作为内部 ID')
        return result


class Store:
    def __init__(self, path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS rules(chat TEXT PRIMARY KEY, mode TEXT NOT NULL,
             body TEXT NOT NULL, enabled INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS contacts(chat TEXT PRIMARY KEY, name TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, message_id TEXT UNIQUE,
             chat TEXT NOT NULL, mode TEXT NOT NULL, instruction TEXT NOT NULL,
             text TEXT, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
             created REAL NOT NULL);
        ''')

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def ingest(self, page):
        if not isinstance(page.get('account_id'), str) or not page['account_id']:
            raise ValueError('消息页缺少账号')
        if not isinstance(page.get('next_cursor'), str) or not isinstance(page.get('has_more'), bool):
            raise ValueError('消息页缺少游标或分页标记')
        with self.db:
            account = self.get('account')
            if account and account != page['account_id']:
                raise ValueError('账号变化，已停止处理')
            self.put('account', page['account_id'])
            baseline = not self.get('initialized', False)
            inserted_count = 0
            reasons = dict(duplicate=0, baseline=0, paused=0, unsupported=0, unmatched=0, queued=0)
            for message in page['messages']:
                if message['account_id'] != page['account_id']:
                    raise ValueError('消息账号与页面不一致')
                inserted = self.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?)',
                    (message['message_id'], json.dumps(message, ensure_ascii=False))).rowcount
                inserted_count += inserted
                reason = 'duplicate' if not inserted else 'baseline' if baseline else 'paused' if not self.get('active', False) else None
                if reason:
                    reasons[reason] += 1
                    continue
                if (message.get('is_self') is not False or not message.get('sender_id')
                        or message.get('type') != 1 or message['chat_id'].endswith('@chatroom')):
                    reasons['unsupported'] += 1
                    continue
                rule = self.db.execute('SELECT * FROM rules WHERE chat=? AND enabled=1',
                                       (message['chat_id'],)).fetchone()
                if rule:
                    reasons['queued'] += 1
                    self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?, ?,?)',
                        (str(uuid.uuid4()), message['message_id'], message['chat_id'],
                         rule['mode'], rule['body'], None, 'pending', '', time.time()))
                else:
                    reasons['unmatched'] += 1
            self.put('cursor', page['next_cursor'])
            self.put('last_ingest_at', time.time())
            self.put('has_more', page['has_more'])
            if page['messages']:
                self.put('last_batch', dict(count=len(page['messages']), reasons=reasons, at=time.time()))
            if baseline:
                self.put('baseline_messages', self.get('baseline_messages', 0) + inserted_count)
                self.put('baseline_pages', self.get('baseline_pages', 0) + 1)
            if baseline and not page['has_more']:
                self.put('initialized', True)

    def update_job(self, job_id, status, detail='', text=None):
        with self.db:
            self.db.execute('UPDATE jobs SET status=?,detail=?,text=COALESCE(?,text) WHERE id=?',
                            (status, detail, text, job_id))

    def save_rule(self, chat, mode, body, enabled, name):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO contacts VALUES (?,?)', (chat, name))
            self.db.execute('INSERT OR REPLACE INTO rules VALUES (?,?,?,?)', (chat, mode, body, enabled))
            self.db.execute("UPDATE jobs SET status='cancelled',detail='规则已修改' WHERE chat=? AND status='pending'",
                            (chat,))

    def claim(self, job, text):
        # Serialize against UI pause/rule edits, then commit before operating the desktop.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rule = self.db.execute('SELECT * FROM rules WHERE chat=? AND enabled=1', (job['chat'],)).fetchone()
            valid = (self.get('active') and rule and rule['mode'] == job['mode']
                     and rule['body'] == job['instruction'])
            if not valid:
                self.db.execute("UPDATE jobs SET status='cancelled',detail='已暂停或规则已修改' WHERE id=? AND status='pending'", (job['id'],))
                self.db.commit()
                return False
            claimed = self.db.execute("UPDATE jobs SET status='sending',text=? WHERE id=? AND status='pending'",
                                      (text, job['id'])).rowcount
            self.db.commit()
            return bool(claimed)
        except Exception:
            self.db.rollback()
            raise

    def snapshot(self):
        return dict(active=self.get('active', False), initialized=self.get('initialized', False),
                    baseline_messages=self.get('baseline_messages', 0),
                    baseline_pages=self.get('baseline_pages', 0),
                    account=self.get('account'), health=self.get('health', {}),
                    error=self.get('error'), checked_at=self.get('checked_at'),
                    stage=self.get('stage'), stage_at=self.get('stage_at'),
                    last_ingest_at=self.get('last_ingest_at'), last_batch=self.get('last_batch'),
                    page_seconds=self.get('page_seconds'), has_more=self.get('has_more', False),
                    job_counts={r[0]: r[1] for r in self.db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')},
                    rules=[dict(r) for r in self.db.execute('SELECT rules.*,contacts.name FROM rules LEFT JOIN contacts USING(chat) ORDER BY chat')],
                    jobs=[dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')])


def generate(db, job):
    """Use a chat-completions HTTP endpoint; credentials stay in the environment."""
    endpoint = os.environ.get('WEREPLY_LLM_URL', '')
    model = os.environ.get('WEREPLY_LLM_MODEL', '')
    if not endpoint or not model:
        raise ValueError('请设置 WEREPLY_LLM_URL 和 WEREPLY_LLM_MODEL')
    if not (endpoint.startswith('https://') or endpoint.startswith('http://127.0.0.1:')
            or endpoint.startswith('http://localhost:')):
        raise ValueError('LLM 地址须使用 HTTPS 或本机 HTTP')
    current = json.loads(db.execute('SELECT data FROM messages WHERE id=?', (job['message_id'],)).fetchone()[0])
    # Context ends at the trigger, so later messages cannot leak into an earlier reply.
    rows = db.execute('''SELECT data FROM messages
        WHERE json_extract(data, '$.chat_id')=?
          AND rowid <= (SELECT rowid FROM messages WHERE id=?)
          AND json_extract(data, '$.type')=1
          AND json_extract(data, '$.is_self') IS NOT NULL
        ORDER BY rowid DESC LIMIT 12''', (job['chat'], job['message_id'])).fetchall()
    history = []
    for row in rows:
        message = json.loads(row[0])
        if (message['chat_id'] == job['chat'] and message.get('is_self') is not None
                and message.get('type') == 1
                and message['timestamp'] <= current['timestamp']):
            history.append({'role': 'assistant' if message['is_self'] else 'user',
                            'content': message['text'][:4000]})
        if len(history) == 12:
            break
    payload = dict(model=model, messages=[{'role': 'system', 'content': job['instruction']}]
                   + list(reversed(history)), max_tokens=512)
    headers = {'Content-Type': 'application/json'}
    if os.environ.get('WEREPLY_LLM_KEY'):
        headers['Authorization'] = 'Bearer ' + os.environ['WEREPLY_LLM_KEY']
    request = urllib.request.Request(endpoint, json.dumps(payload).encode(), headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read(2_000_000))
    text = result['choices'][0]['message']['content']
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('LLM 回复为空或超过 4000 字符')
    return text


class Engine:
    def __init__(self, store, wecli, stopped=lambda: False):
        self.store, self.wecli = store, wecli
        self.stopped = stopped

    def stage(self, name):
        with self.store.db:
            self.store.put('stage', name)
            self.store.put('stage_at', time.time())

    def tick(self):
        s = self.store
        self.stage('checking')
        health = self.wecli.call('doctor', '--format', 'json', allow_failure=True)
        with s.db:
            s.put('health', health)
            s.put('checked_at', time.time())
        if not health.get('read_available') or health.get('account_matches') is not True:
            detail = health.get('read_error') or health.get('account_error') or health.get('error')
            raise ValueError('微信读取不可用或运行账号无法核对' + (f'：{detail}' if detail else ''))
        if s.get('account') and s.get('account') != health.get('account_id'):
            raise ValueError('运行账号与绑定账号不一致')
        limit = '5000' if not s.get('initialized', False) else '500'
        args = ['new-messages', '--limit', limit, '--format', 'json']
        if s.get('cursor'):
            args += ['--cursor', s.get('cursor')]
        self.stage('baseline' if not s.get('initialized') else 'reading')
        started = time.monotonic()
        page = self.wecli.call(*args)
        if page.get('account_id') != health.get('account_id'):
            raise ValueError('读取账号与环境检查不一致')
        if page.get('has_more') and page.get('next_cursor') == s.get('cursor'):
            raise ValueError('消息游标未推进，已停止连续翻页')
        s.ingest(page)
        with s.db:
            s.put('page_seconds', round(time.monotonic() - started, 2))
        self.stage('waiting')
        more = page['has_more']
        if not s.get('active') or not health.get('send_available'):
            return more
        # Only this worker sends, and wecli additionally holds its cross-process lock.
        job = s.db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
        if not job:
            return more
        rule = s.db.execute('SELECT * FROM rules WHERE chat=? AND enabled=1', (job['chat'],)).fetchone()
        if not rule or rule['mode'] != job['mode'] or rule['body'] != job['instruction']:
            s.update_job(job['id'], 'cancelled', '规则已关闭或修改')
            return more
        try:
            self.stage('generating' if job['mode'] == 'llm' else 'preparing')
            text = job['text'] or (job['instruction'] if job['mode'] == 'fixed' else generate(s.db, job))
        except Exception:
            s.update_job(job['id'], 'failed', '生成回复失败，请检查 LLM 配置与服务')
            self.stage('waiting')
            return more
        # Pause may have changed through the UI while generation was in progress.
        if self.stopped() or not s.claim(job, text):
            self.stage('waiting')
            return more
        self.stage('sending')
        try:
            result = self.wecli.call('send', job['chat'], '--file', '-', '--yes',
                                    '--operation-id', job['id'], '--format', 'json',
                                    text=text, allow_failure=True)
            status = result.get('status')
            if status not in ('recorded', 'failed', 'unknown'):
                status = 'unknown'
            s.update_job(job['id'], status, json.dumps(result, ensure_ascii=False))
        except Exception:
            s.update_job(job['id'], 'unknown', '发送调用中断，请核对，禁止自动重发')
        self.stage('waiting')
        return more
