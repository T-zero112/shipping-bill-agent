"""Four deterministic synthetic CLI branches; no model calls, remote OCR or upload."""
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    subprocess.run([sys.executable,'scripts/create_synthetic_demo.py'],cwd=ROOT,check=True)
    fixture=ROOT/'outputs/synthetic-demo';out=ROOT/'outputs/offline-smoke'
    checks=[]
    for case,expected,replay,existing in [
        ('create','CREATE','create.json',None),
        ('update','UPDATE','update.json',fixture/'existing.json'),
        ('no-change','NO_CHANGE','create.json',out/'update/result.json'),
        ('review','PENDING_REVIEW','create.json',fixture/'existing.json')]:
        destination=out/case
        command=[sys.executable,'-m','billing_agent','--input',str(fixture/'input.txt'),'--entity','B',
            '--extractor','replay','--replay',str(fixture/replay),'--output',str(destination)]
        if existing:command+=['--existing',str(existing)]
        run=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
        rows=json.loads((destination/'result.json').read_text(encoding='utf-8'))
        report=json.loads((destination/'run_report.json').read_text(encoding='utf-8'))
        actual=[r['operation'] for r in rows]
        passed=run.returncode==0 and actual==[expected] and report['output_validation']['passed']
        checks.append({'case':case,'expected':expected,'actual':actual,'passed':passed})
        print(case,'PASS' if passed else 'FAIL',flush=True)
    result={'scope':'Synthetic deterministic replay only; no model/OCR accuracy claim','checks':checks,
        'passed':all(c['passed'] for c in checks)}
    (out/'validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
