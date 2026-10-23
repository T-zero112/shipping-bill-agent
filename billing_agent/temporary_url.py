"""Opt-in, content-allowlisted, single-file Cloudflare URL for a bounded OCR call."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

import requests

def file_type(data):
    """Use file signatures, not the supplied filename, for public response types."""
    for signature,extension,mime in [(b'%PDF-','pdf','application/pdf'),
        (b'\x89PNG\r\n\x1a\n','png','image/png'),(b'\xff\xd8\xff','jpg','image/jpeg'),
        (b'II*\x00','tiff','image/tiff'),(b'MM\x00*','tiff','image/tiff')]:
        if data.startswith(signature):return extension,mime
    raise ValueError('Temporary URL supports PDF, PNG, JPEG and TIFF signatures only')


class TemporaryURL:
    def __init__(self, cloudflared, allowed_files, artifacts, interval=30):
        self.binary = Path(cloudflared).resolve()
        if not self.binary.is_file():
            raise ValueError('Portable cloudflared executable required; no automatic download')
        self.allowed = {hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in allowed_files}
        if not self.allowed:
            raise ValueError('Explicit file allowlist required for public temporary URLs')
        self.artifacts = Path(artifacts)
        self.interval = interval

    @contextmanager
    def publish(self, data, name):
        digest = hashlib.sha256(data).hexdigest()
        if digest not in self.allowed:
            raise ValueError('File not explicitly allowed for public temporary URL: '+name)
        extension,mime=file_type(data)
        out = self.artifacts / (digest[:16]+'-'+secrets.token_hex(4))
        out.mkdir(parents=True)
        def save(filename, value):
            (out/filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        route = '/'+secrets.token_urlsafe(24)+'/bill.'+extension
        lock = threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self.serve(False)
            def do_HEAD(self): self.serve(True)
            def serve(self, head):
                valid = self.path == route
                self.send_response(200 if valid else 404)
                self.send_header('Content-Type', mime if valid else 'text/plain')
                self.send_header('Content-Length', str(len(data) if valid else 0))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                written = 0
                if valid and not head:
                    try:
                        self.wfile.write(data)
                        self.wfile.flush()
                        written = len(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                with lock:
                    with (out/'access.jsonl').open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps({'timestamp': time.time(), 'method': self.command,
                            'status': 200 if valid else 404, 'bytes_written': written,
                            'user_agent': self.headers.get('User-Agent'),
                            'probe_label': self.headers.get('X-OCR-Probe'),
                            'cf_connecting_ip': self.headers.get('CF-Connecting-IP')})+'\n')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log = (out/'tunnel.log').open('w', encoding='utf-8')
        process = None
        checks = []
        def check(url, label):
            try:
                r = requests.get(url, headers={'X-OCR-Probe': label}, timeout=(10, 30))
                row = {'label': label, 'timestamp': time.time(), 'status': r.status_code,
                    'content_type': r.headers.get('Content-Type'), 'bytes': len(r.content),
                    'matches_original': r.content == data, 'redirect_count': len(r.history)}
                ok = r.status_code == 200 and row['matches_original']
            except requests.RequestException as exc:
                row = {'label': label, 'error': str(exc), 'matches_original': False}
                ok = False
            checks.append(row)
            save('download-checks.json', checks)
            return ok
        try:
            process = subprocess.Popen([str(self.binary), 'tunnel', '--no-autoupdate', '--protocol',
                'http2', '--url', f'http://127.0.0.1:{server.server_port}'], stdout=log,
                stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            url = None
            for _ in range(90):
                text = (out/'tunnel.log').read_text(encoding='utf-8')
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', text)
                if match and 'Registered tunnel connection' in text:
                    url = match.group()+route
                    break
                if process.poll() is not None:
                    raise RuntimeError('Temporary tunnel exited; see tunnel.log')
                time.sleep(1)
            if not url:
                raise RuntimeError('Temporary tunnel startup timed out')
            save('hosting.json', {'source': name, 'sha256': digest, 'bytes': len(data),
                'url': url, 'lifetime': 'until this context exits'})
            for index in range(3):
                ok=False
                for attempt in range(3):
                    if check(url, f'before-{index+1}-attempt-{attempt+1}'):
                        ok=True
                        break
                    # Allow bounded startup/transport recovery. HTML 200 or any
                    # wrong file content is a hard failure, never accepted.
                    if checks[-1].get('status',503) not in {502,503,504}:
                        break
                    if attempt<2:
                        time.sleep(5)
                if not ok:
                    raise RuntimeError('Temporary URL does not download original bytes; OCR not called')
                if index < 2:
                    time.sleep(self.interval)
            try:
                yield url, out
            finally:
                if not check(url, 'after'):
                    save('postcheck-warning.json', {'warning': 'PDF URL failed verification after OCR'})
        except BaseException as exc:
            save('error.json', {'error': str(exc)})
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
            save('cleanup.json', {'server_closed': True,
                'tunnel_stopped': process is None or process.poll() is not None})
