"""Synthetic Credit Notes and old bills through actual local-model CLI."""
from pathlib import Path
import json
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from billing_agent.__main__ import dump


def main():
    from datetime import datetime
    root=ROOT/'outputs/credit-note/synthetic'/datetime.now().strftime('%Y%m%d-%H%M%S')
    root.mkdir(parents=True,exist_ok=True)
    checks=[]
    for case,entity,amount,expected in [('ap_partial','Beta LLC',30,'CREATE'),('ar_partial','Alpha LLC',30,'CREATE'),
          ('ap_full','Beta LLC',40,'CREATE'),('over_credit','Beta LLC',50,'PENDING_REVIEW'),
          ('missing_original','Beta LLC',30,'PENDING_REVIEW'),('pdf_credit','Beta LLC',30,'PENDING_REVIEW')]:
        folder=root/case;folder.mkdir()
        source=folder/'credit.txt'
        source.write_text(f'CREDIT NOTE No: CN-2001\nOriginal Invoice No: INV-1001\nISSUER: Alpha LLC\nBILL TO: Beta LLC\nHandling Fee USD {amount}',encoding='utf-8')
        if case=='pdf_credit':
            from scripts.run_linking_regression import pdf
            text=source.read_text(encoding='utf-8');source=folder/'credit.pdf'
            pdf(source,[text.splitlines()])
        old={'id':'original-1','document_info':{'document_type':'INVOICE','reference_no':'INV-1001'},
             'transaction_party':{'issuer':'Alpha LLC','recipient':'Beta LLC'},
             'charge_details':{'charge_item':'Handling Charge','amount':40,'currency':'USD'},
             'financial_attributes':{'accounting_entity':entity,'posting_type':'AP' if entity=='Beta LLC' else 'AR'}}
        seed=folder/'existing.json';dump(seed,[] if case=='missing_original' else [old])
        output=folder/'run'
        cmd=[sys.executable,str(ROOT/'scripts/run_local.py'),'--input',str(source),'--existing',str(seed),
             '--entity',entity,'--extractor','llm','--output',str(output)]
        if case=='pdf_credit':cmd+=['--pdf-mode','local']
        if case=='ap_partial':cmd+=['--state-db',str(folder/'ledger.sqlite')]
        run=subprocess.run(cmd,cwd=ROOT,capture_output=True,timeout=600)
        rows=json.loads((output/'result.json').read_text(encoding='utf-8'))
        passed=run.returncode==0 and len(rows)==1 and rows[0]['operation']==expected
        if passed and expected=='CREATE':
            passed &= rows[0]['charge_details']['amount']==-amount and rows[0]['financial_attributes']['posting_type']==old['financial_attributes']['posting_type']
            passed &= rows[0]['credit_adjustment']['remaining_after']==40-amount
        if passed and case=='pdf_credit':
            passed &= rows[0]['financial_attributes']['posting_type']=='AP' and rows[0]['credit_adjustment']['signed_amount']==-30
        checks.append({'case':case,'passed':bool(passed),'expected_operation':expected,'actual':[r['operation'] for r in rows]})
        print(case,'PASS' if passed else 'FAIL',checks[-1]['actual'],flush=True)
        if case=='ap_partial':
            repeat=subprocess.run(cmd,cwd=ROOT,capture_output=True,timeout=600)
            repeated=json.loads((output/'result.json').read_text(encoding='utf-8'))
            ledger=json.loads((output/'ledger-after.json').read_text(encoding='utf-8'))
            checks.append({'case':'state_replay','passed':repeat.returncode==0 and len(repeated)==1 and repeated[0]['operation']=='NO_CHANGE'
                           and len(ledger)==2 and ledger[0]==old})
            print('state_replay','PASS' if checks[-1]['passed'] else 'FAIL',flush=True)
        dump(root/'validation.json',{'dataset':'Synthetic notes and old bills; actual local model; no real credit-note sample provided',
                                     'checks':checks,'passed':len(checks)==7 and all(c['passed'] for c in checks)})
    dump(root.parent/'latest.json',{'report':str(root/'validation.json'),'passed':all(c['passed'] for c in checks)})
    return 0 if all(c['passed'] for c in checks) else 1


if __name__=='__main__':raise SystemExit(main())
