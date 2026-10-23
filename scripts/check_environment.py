"""Read-only dependency check; optional actual CPU OCR and loopback model discovery."""
import argparse
from importlib.metadata import version
import importlib
from io import BytesIO
import json
from pathlib import Path
import platform
import sys
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--ocr-smoke',action='store_true')
    ap.add_argument('--check-ollama',action='store_true')
    ap.add_argument('--output',default='outputs/environment.json')
    args=ap.parse_args()
    checks=[]
    for package,module in [('langchain','langchain'),('langchain-openai','langchain_openai'),
        ('pydantic','pydantic'),('pypdf','pypdf'),('openpyxl','openpyxl'),('python-docx','docx'),
        ('xlrd','xlrd'),('pycountry','pycountry'),('pdfplumber','pdfplumber'),
        ('rapidocr-onnxruntime','rapidocr_onnxruntime'),('pypdfium2','pypdfium2'),('requests','requests')]:
        try:
            importlib.import_module(module)
            checks.append({'package':package,'version':version(package),'passed':True})
        except Exception as exc:
            checks.append({'package':package,'passed':False,'error':str(exc)})
    external={}
    from billing_agent.conversion import soffice_path
    try:external['libreoffice']={'available':True,'path':soffice_path()}
    except RuntimeError as exc:external['libreoffice']={'available':False,'note':str(exc)}
    if args.check_ollama:
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open('http://127.0.0.1:11434/api/tags',timeout=5) as r:models=json.load(r)
            external['ollama']={'available':True,'models':[m['name'] for m in models.get('models',[])],
                'note':'Discovery only; not model generation verification'}
        except Exception as exc:external['ollama']={'available':False,'error':str(exc)}
    if args.ocr_smoke:
        try:
            from PIL import Image,ImageDraw,ImageFont
            from billing_agent.local_ocr import LocalOCR
            image=Image.new('RGB',(900,180),'white')
            ImageDraw.Draw(image).text((30,40),'INVOICE TEST Handling Fee USD 40',font=ImageFont.load_default(size=30),fill='black')
            stream=BytesIO();image.save(stream,format='PNG')
            ocr=LocalOCR().recognize(stream.getvalue())
            text=' '.join(b['text'] for frame in ocr['frames'] for b in frame['boxes'])
            checks.append({'check':'actual_cpu_ocr_synthetic','passed':bool(text),'recognized_text':text})
        except Exception as exc:checks.append({'check':'actual_cpu_ocr_synthetic','passed':False,'error':str(exc)})
    result={'python':platform.python_version(),'platform':platform.platform(),'isolated_venv':sys.prefix!=sys.base_prefix,
        'checks':checks,'external':external,'passed':all(c['passed'] for c in checks),
        'note':'Existing OS and optional services are not installed by this check; no remote file upload'}
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
