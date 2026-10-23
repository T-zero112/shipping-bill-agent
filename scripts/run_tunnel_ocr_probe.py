"""Explicitly invoked, single-file public OCR probe; shuts down its tunnel on exit."""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--cloudflared', required=True)
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    content = Path(args.file).read_bytes()
    if not content.startswith(b'%PDF-'):
        raise ValueError('This probe only serves an explicitly selected PDF')
    digest = hashlib.sha256(content).hexdigest()
    route = '/' + secrets.token_urlsafe(24) + '/bill.pdf'
    lock = threading.Lock()

    def save(name, value):
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.serve(False)

        def do_HEAD(self):
            self.serve(True)

        def serve(self, head):
            valid = self.path == route
            status = 200 if valid else 404
            with lock:
                with (out / 'access.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'method': self.command, 'valid_file_path': valid, 'status': status,
                        'user_agent': self.headers.get('User-Agent'),
                        'cf_connecting_ip': self.headers.get('CF-Connecting-IP'),
                        'probe_label': self.headers.get('X-OCR-Probe'),
                        'bytes_available': len(content) if valid else 0}) + '\n')
            self.send_response(status)
            self.send_header('Content-Type', 'application/pdf' if valid else 'text/plain')
            self.send_header('Content-Length', str(len(content) if valid else 0))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if valid and not head:
                try:
                    self.wfile.write(content)
                    self.wfile.flush()
                    with lock:
                        with (out / 'transfers.jsonl').open('a', encoding='utf-8') as stream:
                            stream.write(json.dumps({'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                'user_agent': self.headers.get('User-Agent'), 'probe_label': self.headers.get('X-OCR-Probe'),
                                'bytes_written': len(content)}) + '\n')
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log = (out / 'tunnel.log').open('w', encoding='utf-8')
    process = None
    checks = []
    start = time.monotonic()
    try:
        process = subprocess.Popen([str(Path(args.cloudflared).resolve()), 'tunnel', '--no-autoupdate',
            '--protocol', 'http2', '--url', f'http://127.0.0.1:{server.server_port}'],
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        host = None
        for _ in range(90):
            log.flush()
            text = (out / 'tunnel.log').read_text(encoding='utf-8')
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', text)
            if match and 'Registered tunnel connection' in text:
                host = match.group()
                break
            if process.poll() is not None:
                raise RuntimeError('Tunnel exited; see tunnel.log')
            time.sleep(1)
        if not host:
            raise RuntimeError('Tunnel did not register in 90 seconds; see tunnel.log')
        url = host + route
        save('hosting.json', {'source': str(Path(args.file).resolve()), 'sha256': digest,
            'bytes': len(content), 'url': url, 'public_access': 'Only the random PDF path; no directory listing',
            'lifetime': 'Until this probe exits; not permanent storage'})

        def check(label):
            try:
                response = requests.get(url, headers={'X-OCR-Probe': label}, timeout=(10, 30))
                item = {'label': label, 'elapsed_seconds': round(time.monotonic()-start, 2),
                    'status': response.status_code, 'bytes': len(response.content),
                    'content_type': response.headers.get('content-type'),
                    'redirect_count': len(response.history),
                    'sha256': hashlib.sha256(response.content).hexdigest(),
                    'matches_original': response.content == content}
            except requests.RequestException as exc:
                item = {'label': label, 'error': str(exc), 'matches_original': False}
            checks.append(item)
            save('download-checks.json', checks)
            print(json.dumps(item), flush=True)
            return item['matches_original'] and item.get('status') == 200

        for i in range(3):
            if not check(f'before-{i+1}'):
                raise RuntimeError('Public download verification failed; OCR not called')
            if i < 2:
                time.sleep(30)
        payload = {'file': url, 'task': 'orderChange', 'callBack': '', 'taskType': ''}
        save('ocr-request.json', payload)
        print('Public PDF checks passed; submitting designated OCR', flush=True)

        def call():
            called = time.monotonic()
            try:
                response = requests.post('http://36.103.199.11:5002/process', json=payload, timeout=(10, 300))
                (out / 'ocr-response.txt').write_bytes(response.content)
                save('ocr-http.json', {'status': response.status_code,
                    'elapsed_seconds': round(time.monotonic()-called, 2), 'headers': dict(response.headers)})
                print(response.status_code, response.content.decode('utf-8', errors='replace')[:16000], flush=True)
            except requests.RequestException as exc:
                save('ocr-error.json', {'error': str(exc), 'elapsed_seconds': round(time.monotonic()-called, 2)})
                print('OCR request failed:', str(exc), flush=True)

        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(call)
            i = 0
            while True:
                try:
                    future.result(timeout=40)
                    break
                except concurrent.futures.TimeoutError:
                    i += 1
                    check(f'during-{i}')
        check('after')
    except Exception as exc:
        save('probe-error.json', {'error': str(exc)})
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        log.close()
        save('cleanup.json', {'server_closed': True, 'tunnel_stopped': process is None or process.poll() is not None,
            'url_no_longer_served': True})


if __name__ == '__main__':
    main()
