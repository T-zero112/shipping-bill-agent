"""Loopback-only demonstration server; subprocess jobs use the existing CLI."""
import base64
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlsplit
import uuid

ROOT=Path(__file__).resolve().parents[1]
STATIC=Path(__file__).with_name('web')
ALLOWED={'.pdf','.eml','.png','.jpg','.jpeg','.tif','.tiff','.doc','.docx','.xls','.xlsx','.txt'}
DOWNLOADS={'result.json','evidence.json','run_report.json','extraction.json','remote-ocr-adapter.json'}


class DemoApp:
    def __init__(self, directory, cloudflared=None):
        self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True)
        self.cloudflared=str(Path(cloudflared).resolve()) if cloudflared else None
        if self.cloudflared and not Path(self.cloudflared).is_file():raise ValueError('cloudflared not found')
        self.token=secrets.token_urlsafe(32)
        self.jobs={};self.lock=threading.Lock();self.pool=ThreadPoolExecutor(1)

    def submit(self, payload):
        entity=str(payload.get('entity','')).strip()
        synthetic=payload.get('synthetic') is True
        if not entity or len(entity)>200:raise ValueError('请填写当前记账主体（不超过 200 字）')
        mode=payload.get('mode','local')
        if mode not in {'local','remote'}:raise ValueError('不支持的处理方式')
        if synthetic:
            name='synthetic.txt';data=b'A charges B. DN1 Handling Fee USD 40\nDocumentation Fee: amount not stated'
            entity='B';mode='local'
        else:
            name=str(payload.get('filename','')).replace('\\','/').split('/')[-1]
            if not name or len(name)>180 or Path(name).suffix.lower() not in ALLOWED:
                raise ValueError('文件名过长或格式不支持')
            try:data=base64.b64decode(payload.get('content',''),validate=True)
            except Exception:raise ValueError('上传内容编码无效')
            if not data or len(data)>30*1024*1024:raise ValueError('请选择不超过 30 MB 的文件')
        if mode=='remote':
            if not self.cloudflared:raise ValueError('启动服务时尚未配置 cloudflared')
            if not data.startswith(b'%PDF-'):raise ValueError('临时 URL 演示目前仅支持 PDF')
            if payload.get('consent') is not True:raise ValueError('请明确同意通过临时公网 URL 提供此 PDF')
        with self.lock:
            if any(j['state'] in {'queued','running'} for j in self.jobs.values()):
                raise ValueError('已有文件正在处理，请等待当前任务结束')
            identity=uuid.uuid4().hex;folder=self.directory/identity;folder.mkdir()
            source=folder/name;source.write_bytes(data)
            self.jobs[identity]={'id':identity,'state':'queued','filename':name,'entity':entity,
                'mode':mode,'synthetic':synthetic,'output':folder/'result','error':None}
        self.pool.submit(self.run,identity,source)
        return identity

    def run(self, identity, source):
        job=self.jobs[identity];job['state']='running';out=job['output']
        command=[sys.executable,'-m','billing_agent','--input',str(source),'--entity',job['entity'],
            '--output',str(out)]
        if job['synthetic']:
            from .parsers import Parser
            p=Parser();p.parse(source.read_bytes(),source.name)
            fixture={'charges':[{'reference_no':'DN1','charge_item':'Handling Fee','amount':40,'currency':'USD',
                'issuer':'A','recipient':'B','evidence_ids':[p.evidence[0].id],'amount_quote':'USD 40',
                'direction':'ISSUER_CHARGES_RECIPIENT','direction_quote':'A charges B.'},
                {'reference_no':'DN1','charge_item':'Documentation Fee','amount':None,'currency':None,
                 'evidence_ids':[p.evidence[0].id],'name_quote':'Documentation Fee',
                 'warnings':['Synthetic review example: amount and currency are not stated in the source']} ]}
            replay=source.parent/'replay.json';replay.write_text(json.dumps(fixture),encoding='utf-8')
            command+=['--extractor','replay','--replay',str(replay)]
        elif job['mode']=='remote':
            command+=['--pdf-mode','ocr','--extractor','remote','--temporary-url-file',str(source),
                '--cloudflared',self.cloudflared]
        else:
            command+=['--pdf-mode','local','--image-mode','local','--extractor','llm',
                '--local-ocr-cache',str(self.directory/'ocr-cache')]
        try:
            # No server timeout kill: allow CLI finally blocks to clean up public tunnels.
            env={**os.environ,'BILLING_PROVIDER':'ollama','PYTHONIOENCODING':'utf-8'}
            if not env.get('BILLING_MODEL'):env['BILLING_MODEL']='qwen3:8b'
            result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True)
            (source.parent/'process.log').write_bytes(result.stdout+result.stderr)
            job['exit_code']=result.returncode
            job['state']='done' if (out/'run_report.json').is_file() else 'failed'
            if job['state']=='failed':job['error']='处理程序未生成报告，请检查本机 process.log'
        except Exception as exc:
            job['state']='failed';job['error']=str(exc)

    def snapshot(self, identity):
        with self.lock:
            job=dict(self.jobs[identity])
        out=job.pop('output')
        if job['state']=='done':
            for key,name in [('report','run_report.json'),('results','result.json'),('evidence','evidence.json')]:
                path=out/name
                job[key]=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else ([] if key!='report' else {})
        return job


def make_server(app,port=8767):
    class Handler(BaseHTTPRequestHandler):
        def reply(self,status,body,content_type='application/json; charset=utf-8',attachment=None):
            data=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            if attachment:self.send_header('Content-Disposition',f'attachment; filename="{attachment}"')
            self.end_headers();self.wfile.write(data)
        def valid_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        def authenticated(self):return self.headers.get('X-Demo-Token')==app.token
        def do_GET(self):
            if not self.valid_host():return self.reply(403,{'error':'Invalid host'})
            path=urlsplit(self.path).path
            if path=='/api/config':return self.reply(200,{'token':app.token,'remote_available':bool(app.cloudflared)})
            if path.startswith('/api/'):
                if not self.authenticated():return self.reply(403,{'error':'Session token required'})
                parts=path.strip('/').split('/')
                if len(parts) not in {3,4} or parts[:2]!=['api','jobs'] or parts[2] not in app.jobs:
                    return self.reply(404,{'error':'找不到任务'})
                if len(parts)==3:return self.reply(200,app.snapshot(parts[2]))
                name=parts[3]
                if name not in DOWNLOADS:return self.reply(404,{'error':'不可下载此文件'})
                file=app.jobs[parts[2]]['output']/name
                if not file.is_file():return self.reply(404,{'error':'结果尚未生成'})
                return self.reply(200,file.read_bytes(),attachment=name)
            files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if path not in files:return self.reply(404,{'error':'Not found'})
            mime={'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}
            return self.reply(200,(STATIC/files[path]).read_bytes(),mime[path])
        def do_POST(self):
            if not self.valid_host() or not self.authenticated():return self.reply(403,{'error':'Invalid local session'})
            if self.headers.get('Origin') not in {f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}:
                return self.reply(403,{'error':'Origin rejected'})
            if self.path!='/api/jobs':return self.reply(404,{'error':'Not found'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if size<=0 or size>42*1024*1024:raise ValueError('上传请求超过大小限制')
                payload=json.loads(self.rfile.read(size))
                if not isinstance(payload,dict):raise ValueError('请求格式无效')
                identity=app.submit(payload)
                return self.reply(202,{'id':identity})
            except (ValueError,TypeError) as exc:return self.reply(400,{'error':str(exc)})
        def log_message(self,*args):pass
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)
