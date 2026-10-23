"""Actual synthetic two-page PDFs through local-model CLI; no external upload."""
from pathlib import Path
import json
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from billing_agent.__main__ import dump


def pdf(path,pages):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    writer=PdfWriter()
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    for lines in pages:
        page=writer.add_blank_page(612,792)
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        commands=['BT /F1 12 Tf 40 750 Td']
        for line in lines:
            safe=line.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
            commands.append('('+safe+') Tj 0 -28 Td')
        commands.append('ET')
        stream=DecodedStreamObject();stream.set_data('\n'.join(commands).encode('ascii'))
        page[NameObject('/Contents')]=writer._add_object(stream)
    with path.open('wb') as f:writer.write(f)


def main():
    root=ROOT/'outputs/linking/synthetic';root.mkdir(parents=True,exist_ok=True)
    checks=[]
    header=['INVOICE No: TEST-1001','INVOICE DATE: 2026-10-22','ISSUER: Alpha LLC','BILL TO: Beta LLC',
            'MASTER B/L NO: MASTER123','HOUSE B/L NO: HOUSE123','CNTR. NO.: ABCD1234567, EFGH7654321']
    for case in ['same_document','different_document','missing_reference','conflicting_date']:
        second=['INVOICE No: '+('OTHER-1002' if case=='different_document' else 'TEST-1001'),
                'Handling Fee USD 40']
        if case=='missing_reference':second=['INVOICE - CONTINUED','Handling Fee USD 40','Synthetic test data only']
        if case=='conflicting_date':second.insert(1,'INVOICE DATE: 2026-10-23')
        source=root/(case+'.pdf');pdf(source,[header,second])
        output=root/case
        cmd=[sys.executable,str(ROOT/'scripts/run_local.py'),'--input',str(source),'--entity','Beta LLC',
             '--pdf-mode','local','--extractor','llm','--output',str(output)]
        run=subprocess.run(cmd,cwd=ROOT,capture_output=True,timeout=600)
        rows=json.loads((output/'result.json').read_text(encoding='utf-8'))
        report=json.loads((output/'run_report.json').read_text(encoding='utf-8'))
        passed=run.returncode==0 and len(rows)==1 and rows[0]['charge_details']['amount']==40 and report['output_validation']['passed']
        if passed:
            row=rows[0]
            if case=='same_document':
                passed &= row['financial_attributes']['posting_type']=='AP' and row['transaction_party']['issuer']=='Alpha LLC'
                passed &= row['transport_info']['master_bills']==['MASTER123'] and len(row['transport_info']['containers'])==2
            else:
                passed &= row['transaction_party']['issuer'] is None and not row['transport_info']['master_bills']
        checks.append({'case':case,'passed':bool(passed),'fee_count':len(rows),'report':str(output/'run_report.json')})
        print(case,'PASS' if passed else 'FAIL',flush=True)
        dump(root/'validation.json',{'scope':'Generated PDFs, actual local model calls, no company history or remote OCR',
             'checks':checks,'passed':len(checks)==4 and all(c['passed'] for c in checks)})
    return 0 if all(c['passed'] for c in checks) else 1


if __name__=='__main__':raise SystemExit(main())
