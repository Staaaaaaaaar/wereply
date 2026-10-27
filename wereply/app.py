import argparse
import fcntl
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .core import Engine, Store, Wecli


def main():
    parser = argparse.ArgumentParser(description='wereply 本地自动回复控制台')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', default='~/.local/share/wereply/state.db')
    parser.add_argument('--wecli', default='wecli', help='命令，例如 /path/python -m wecli')
    parser.add_argument('--wecli-config')
    parser.add_argument('--interval', type=float, default=3)
    args = parser.parse_args()
    if args.interval < 1 or not 1 <= args.port <= 65535:
        parser.error('interval 必须 >= 1，port 必须在 1–65535 之间')
    os.umask(0o077)
    try:
        client = Wecli(args.wecli, args.wecli_config)
    except ValueError as exc:
        parser.error(str(exc))
    path = Path(args.db).expanduser().resolve()
    store = Store(path)
    lock = open(str(path) + '.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('该状态库已有 wereply 运行')
    with store.db:
        if store.get('baseline_messages') is None:
            store.put('baseline_messages', store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
        store.put('active', False)  # Every launch requires explicit enablement.
        store.db.execute("UPDATE jobs SET status='unknown', detail='服务在发送中停止，请核对' WHERE status='sending'")
        store.db.execute("UPDATE jobs SET status='cancelled', detail='服务重新启动' WHERE status='pending'")
    store.db.close()
    token = secrets.token_urlsafe(32)
    stop = threading.Event()
    origin = f'http://127.0.0.1:{args.port}'

    def worker():
        local = Store(path)
        engine = Engine(local, client, stop.is_set)
        try:
            while not stop.is_set():
                more = False
                try:
                    more = engine.tick()
                    with local.db:
                        local.put('error', None)
                except Exception as exc:
                    with local.db:
                        local.put('error', str(exc)[:500])
                        local.put('stage', 'error')
                # Catch up immediately; apply the polling interval only at the tail or on error.
                stop.wait(0 if more else args.interval)
        finally:
            local.db.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, value, mime='application/json; charset=utf-8'):
            data = json.dumps(value, ensure_ascii=False).encode() if mime.startswith('application/json') else value
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{args.port}'

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {'error': 'Host rejected'})
            if self.path == '/api/state':
                local = Store(path)
                try:
                    state = local.snapshot()
                    state['token'] = token
                    return self.respond(200, state)
                finally:
                    local.db.close()
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if self.path not in files:
                return self.respond(404, {'error': 'Not found'})
            name, mime = files[self.path]
            self.respond(200, (Path(__file__).parent / 'static' / name).read_bytes(), mime)

        def do_POST(self):
            if (not self.valid_host() or self.headers.get('Origin') not in (None, origin)
                    or self.headers.get('X-Wereply-Token') != token):
                return self.respond(403, {'error': '请求来源或令牌无效'})
            local = Store(path)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 20000:
                    raise ValueError('请求大小无效')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('请求必须为对象')
                with local.db:
                    if self.path == '/api/active':
                        if type(body.get('active')) is not bool:
                            raise ValueError('开关值无效')
                        if body['active'] and not local.get('initialized'):
                            raise ValueError('请等待首次历史基线完成')
                        local.put('active', body['active'])
                        if not body['active']:
                            local.db.execute("UPDATE jobs SET status='cancelled',detail='已暂停' WHERE status='pending'")
                    elif self.path == '/api/contacts':
                        query = body.get('query', '')
                        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
                            raise ValueError('请输入昵称、备注或内部 ID 搜索')
                        return self.respond(200, {'contacts': client.contacts(query.strip())})
                    elif self.path == '/api/rules/delete':
                        chat = body.get('chat')
                        if not isinstance(chat, str):
                            raise ValueError('联系人 ID 无效')
                        local.db.execute('DELETE FROM rules WHERE chat=?', (chat,))
                        local.db.execute("UPDATE jobs SET status='cancelled',detail='规则已删除' WHERE chat=? AND status='pending'", (chat,))
                    elif self.path == '/api/rules':
                        chat, mode, text = body.get('chat'), body.get('mode'), body.get('body')
                        if (not isinstance(chat, str) or not chat.strip() or len(chat) > 200
                                or chat.endswith('@chatroom') or any(c.isspace() for c in chat)
                                or mode not in ('fixed', 'llm') or not isinstance(text, str)
                                or not text.strip() or len(text) > 4000
                                or type(body.get('enabled')) is not bool):
                            raise ValueError('请填写私聊联系人 ID、回复内容及有效模式')
                        # Allow disabling an old invalid rule without requiring a successful lookup.
                        if not body['enabled'] and local.db.execute('SELECT 1 FROM rules WHERE chat=?', (chat,)).fetchone():
                            name = chat
                        else:
                            contact = client.contact(chat)
                            name = contact.get('remark') or contact.get('nick_name') or chat
                        local.save_rule(chat, mode, text, body['enabled'], name)
                    elif self.path == '/api/reconcile':
                        job = local.db.execute('SELECT * FROM jobs WHERE id=?', (body.get('id'),)).fetchone()
                        if not job or job['status'] != 'unknown':
                            raise ValueError('只能核对结果未知的任务')
                        health = client.call('doctor', '--format', 'json', allow_failure=True)
                        if health.get('account_id') != local.get('account') or health.get('account_matches') is not True:
                            raise ValueError('账号未匹配，无法核对')
                        result = client.call('send-status', job['id'], '--reconcile', allow_failure=True)
                        status = result.get('status', 'unknown')
                        if status not in ('recorded', 'failed', 'unknown'):
                            status = 'unknown'
                        local.update_job(job['id'], status, json.dumps(result, ensure_ascii=False))
                    else:
                        return self.respond(404, {'error': 'Not found'})
                self.respond(200, {'ok': True})
            except (ValueError, KeyError, TypeError) as exc:
                self.respond(400, {'error': str(exc)})
            except Exception:
                self.respond(500, {'error': '操作失败，请检查 wecli 与服务状态'})
            finally:
                local.db.close()

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    thread = threading.Thread(target=worker)
    thread.start()
    print(f'wereply: {origin}（实际发送默认关闭）', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        stop.set()
        # A current wecli call may take 120 seconds; the worker checks stop before sending.
        thread.join()
        lock.close()


if __name__ == '__main__':
    main()
