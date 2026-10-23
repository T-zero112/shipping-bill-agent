"""Start the local bill review UI; does not open a public tunnel by default."""
import argparse
import os
import shutil
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from billing_agent.webapp import DemoApp,make_server

ROOT=Path(__file__).resolve().parents[1]

def resolve_cloudflared(explicit=None, enabled=False):
    """Enable only by request; resolve portable binary without downloading it."""
    if explicit:
        path=Path(explicit).expanduser().resolve()
        if not path.is_file():raise ValueError(f'cloudflared not found: {path}')
        return str(path)
    if not enabled:return None
    candidates=[os.environ.get('BILLING_CLOUDFLARED'),shutil.which('cloudflared'),
                str(ROOT/'outputs/remote-ocr-tunnel/cloudflared.exe')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():return str(Path(candidate).resolve())
    raise ValueError('指定 OCR 需要 cloudflared：请通过 --cloudflared 路径或 BILLING_CLOUDFLARED 配置官方程序')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--data-dir',default='outputs/web-demo')
    parser.add_argument('--cloudflared',help='Optional portable executable; enables explicitly consented PDF URL mode')
    parser.add_argument('--designated-ocr',action='store_true',help='Enable designated OCR using configured/PATH/project portable cloudflared')
    args=parser.parse_args()
    try:cloudflared=resolve_cloudflared(args.cloudflared,args.designated_ocr)
    except ValueError as exc:parser.error(str(exc))
    app=DemoApp(args.data_dir,cloudflared);server=make_server(app,args.port)
    print(f'Bill demo: http://127.0.0.1:{server.server_port}  (Ctrl+C to stop; wait for active jobs first)',flush=True)
    print('Designated OCR: '+('enabled (PDF consent required; tunnel starts per job)' if cloudflared else 'disabled; enable with --designated-ocr or --cloudflared PATH'),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.server_close();app.pool.shutdown(wait=True)
