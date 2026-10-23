"""Offline CLI status acceptance: synthetic evidence and explicit replay fixtures."""
import json
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from billing_agent.parsers import Parser
from billing_agent.__main__ import dump


def main():
    root=ROOT/'outputs/status-contract/scenarios';root.mkdir(parents=True,exist_ok=True)
    checks=[]
    for case,status,code in [('success','SUCCESS',0),('review','PENDING_REVIEW',0),('empty','EMPTY_RESULT',0),
                             ('parse_failure','PARSE_FAILED',2),('partial','PARTIAL_FAILURE',2),
                             ('extract_failure','EXTRACTION_FAILED',2),('not_extracted','NOT_EXTRACTED',0),
                             ('input_invalid','INPUT_INVALID',2),('remote_empty','UNRESOLVED',2)]:
        folder=root/case;folder.mkdir(exist_ok=True);inputs=folder/'inputs';inputs.mkdir(exist_ok=True)
        raw=b'A charges B. DN1 Handling Fee USD 40'
        if case in {'parse_failure','partial'}:(inputs/'broken.doc').write_bytes(b'not a DOC')
        if case!='parse_failure':(inputs/'good.txt').write_bytes(raw)
        p=Parser();p.parse(raw,'good.txt');e=p.evidence[0]
        charge={'reference_no':'DN1','charge_item':'Handling Fee','amount':40,'currency':'USD',
                'issuer':'A','recipient':'B','direction':'ISSUER_CHARGES_RECIPIENT',
                'direction_quote':'A charges B.','evidence_ids':[e.id],'amount_quote':'USD 40'}
        if case=='review':charge['direction']='UNKNOWN';charge['direction_quote']=None
        replay=folder/'replay.json';dump(replay,{'charges':[charge] if case in {'success','review','partial'} else []})
        if case=='extract_failure':replay.write_text('not JSON',encoding='utf-8')
        output=folder/'run'
        command=[sys.executable,'-m','billing_agent','--input',str(inputs),'--entity','B',
                 '--output',str(output),'--extractor','none' if case=='not_extracted' else 'replay','--replay',str(replay)]
        if case=='input_invalid':
            bad=folder/'bad-existing.json';dump(bad,{})
            command+=['--existing',str(bad)]
        if case=='remote_empty':
            import hashlib
            image=b'synthetic empty OCR fixture'
            (inputs/'probe.png').write_bytes(image)
            cache=folder/'ocr-cache.json'
            dump(cache,{hashlib.sha256(image).hexdigest():{'success':True,'data':{'list':[],'pages_split':[]}}})
            command+=['--ocr-cache',str(cache)]
        run=subprocess.run(command,cwd=ROOT,capture_output=True,timeout=30)
        report=json.loads((output/'run_report.json').read_text(encoding='utf-8'))
        passed=report['status']==status and run.returncode==code and (output/'result.schema.json').exists()
        checks.append({'case':case,'expected_status':status,'actual_status':report['status'],
                       'exit_code':run.returncode,'passed':passed})
        print(case,report['status'],'PASS' if passed else 'FAIL',flush=True)
    report={'scope':'Offline synthetic CLI fixtures; not model or remote service verification',
            'checks':checks,'passed':all(c['passed'] for c in checks)}
    dump(root/'validation.json',report)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
