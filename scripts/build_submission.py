"""Allowlisted source-only ZIP. No real documents, private logs or local runtimes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--output',default='outputs/delivery/billing-agent-submission.zip')
    args=ap.parse_args()
    output=Path(args.output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    entries={}
    for folder in ['billing_agent','tests','scripts']:
        for file in sorted((ROOT/folder).rglob('*.py')):
            if '__pycache__' in file.parts or file.name=='run_with_resume_config.py' or file.name.startswith('validate_'):
                continue  # Omit personal cross-project config helper and real-sample gold validators.
            entries[file.relative_to(ROOT).as_posix()]=file.read_bytes()
    for name in ['README.md','REQUIREMENTS_COVERAGE.md','ARCHITECTURE.md','REPRODUCTION_REPORT.md',
                 'WEB_DEMO_REPORT.md','ACCEPTANCE_RECORD.md','DESIGN_REFLECTIONS.md','requirements.txt','requirements.lock.txt','pytest.ini','.env.example','.gitignore']:
        entries[name]=(ROOT/name).read_bytes()
    for file in sorted((ROOT/'billing_agent/web').iterdir()):
        if file.suffix in {'.html','.js','.css'}:
            entries[file.relative_to(ROOT).as_posix()]=file.read_bytes()
    subprocess.run([sys.executable,str(ROOT/'scripts/create_synthetic_demo.py')],cwd=ROOT,check=True,capture_output=True)
    for name in ['input.txt','create.json','update.json','existing.json']:
        entries['examples/'+name]=(ROOT/'outputs/synthetic-demo'/name).read_bytes()
    entries['examples/README.txt']=b'Synthetic data only. Run scripts/run_offline_smoke.py for four CLI operations. Not model output.\n'
    forbidden=[b'C:/Users/zx',b'C:\\Users\\zx',b'NEPTUNE SHIPPING',b'AEBO TCO',b'MAEU275752922',b'DC-108478',b'trycloudflare.com/']
    for name,data in entries.items():
        if name=='scripts/build_submission.py':
            continue  # The deny-pattern literals are not actual private records.
        if any(token in data for token in forbidden):
            raise ValueError('Potential private sample/local path in allowlisted file: '+name)
    manifest={'scope':'Source, synthetic tests/examples and sanitized delivery documents only',
        'excluded':['real samples','OCR responses/access logs','private config helper','real sample gold validators',
                    'historical reports','virtualenvs','caches','model weights','cloudflared binary','.git'],
        'files':{name:hashlib.sha256(data).hexdigest() for name,data in sorted(entries.items())}}
    entries['MANIFEST.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(entries.items()):
            info=zipfile.ZipInfo('billing-agent/'+name,date_time=(2026,10,22,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,data)
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest+'  '+output.name+'\n',encoding='utf-8')
    print(json.dumps({'zip':str(output),'files':len(entries),'bytes':output.stat().st_size,'sha256':digest}))


if __name__=='__main__':raise SystemExit(main())
