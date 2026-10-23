"""Synthetic EML + old-ledger CLI acceptance checks; no external mail is sent."""
from copy import deepcopy
from datetime import datetime
from email.message import EmailMessage
import json
from pathlib import Path
import subprocess
import sys

PROJECT=Path(__file__).resolve().parents[1]


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    root=PROJECT/'outputs'/'amendment-regression'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    root.mkdir(parents=True)
    base={'id':'synthetic-bill-1','document_info':{'document_type':'Invoice','reference_no':'DN-1001','document_date':'2026-01-01'},
        'transaction_party':{'issuer':'Alpha LLC','recipient':'Beta LLC'},
        'charge_details':{'charge_item':'Handling Charge','amount':30,'currency':'USD','payment_term':'PP'},
        'financial_attributes':{'accounting_entity':'Beta LLC','posting_type':'AP'},'protected_note':'keep exactly'}
    checks=[]
    def run(name,text,expected,field=None,value=None,seed=None,state=None,message_id=None,input_path=None):
        case=root/name;case.mkdir()
        seed=deepcopy(seed if seed is not None else [base])
        save(case/'existing.json',seed)
        if input_path is None:
            message=EmailMessage();message['Subject']='SYNTHETIC bill amendment'
            message['From']='sender@example.invalid';message['To']='finance@example.invalid'
            message['Message-ID']=f'<{message_id or name}@example.invalid>'
            message.set_content(text)
            input_path=case/'request.eml';input_path.write_bytes(message.as_bytes())
        cmd=[sys.executable,str(PROJECT/'scripts/run_local.py'),'--input',str(input_path),
            '--entity','Beta LLC','--existing',str(case/'existing.json'),'--extractor','llm',
            '--state-db',str(state or case/'ledger.sqlite'),'--output',str(case/'run')]
        result=subprocess.run(cmd,cwd=PROJECT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=360)
        if result.returncode:
            raise RuntimeError(f'{name}: CLI failed: {result.stderr[-1500:]}')
        rows=json.loads((case/'run/result.json').read_text(encoding='utf-8'))
        ledger=json.loads((case/'run/ledger-after.json').read_text(encoding='utf-8'))
        report=json.loads((case/'run/run_report.json').read_text(encoding='utf-8'))
        operations=[r['operation'] for r in rows]
        passed=operations==expected and not report['extraction_error'] and not report['extraction_errors']
        if field:
            wanted=deepcopy(base);wanted['charge_details'][field]=value
            passed &= ledger==[wanted]
            if rows and expected==['UPDATE']:
                passed &= set(rows[0]['audit_trail']['proposed_changes'])=={field}
        else:
            passed &= ledger==seed
        checks.append({'case':name,'operations':operations,'passed':bool(passed),
            'state':report['state'],'successful_local_model_calls':report['successful_local_model_calls'],
            'amendments':report['amendment_count'],'artifacts':str(case/'run')})
        print(name,operations,'PASS' if passed else 'FAIL',flush=True)
        return case,ledger,rows
    amount='请将账单 DN-1001 的 Handling Fee 金额改为 USD 40，其他不变。'
    run('amount_only',amount,['UPDATE'],'amount',40)
    run('term_only','Please change bill DN-1001 Handling Fee payment term to CC; other fields unchanged.', ['UPDATE'],'payment_term','CC')
    run('currency_only','Please change bill DN-1001 Handling Fee currency to EUR; leave amount unchanged.', ['UPDATE'],'currency','EUR')
    run('observe_without_change_instruction','Bill DN-1001 Handling Fee USD 40.', ['PENDING_REVIEW'])
    duplicate=deepcopy(base);duplicate['id']='synthetic-bill-2';duplicate['charge_details']['amount']=35
    run('ambiguous_same_name',amount,['PENDING_REVIEW'],seed=[base,duplicate])
    other=deepcopy(base);other['financial_attributes']['accounting_entity']='Other LLC'
    run('other_accounting_entity',amount,['PENDING_REVIEW'],seed=[other])
    run('missing_target',amount.replace('DN-1001','DN-9999'),['PENDING_REVIEW'])
    run('unauthorized_currency_context',amount.replace('USD','EUR'),['PENDING_REVIEW'])
    run('conditional','If approved, change bill DN-1001 Handling Fee amount to USD 40.',['PENDING_REVIEW'])
    run('negative','Do not change bill DN-1001 Handling Fee amount to USD 40.',['PENDING_REVIEW'])
    run('clear_field','Please change bill DN-1001 Handling Fee payment term to null.',['PENDING_REVIEW'])
    run('quoted_history','No changes requested.\n> '+amount,[])
    # One persistent local ledger for exact replay and stale-mail replay after a newer update.
    state=root/'persistent-ledger.sqlite'
    first,_,_=run('persistent_first',amount,['UPDATE'],'amount',40,state=state,message_id='persistent-first')
    run('persistent_repeat',amount,['NO_CHANGE'],'amount',40,state=state,input_path=first/'request.eml')
    run('persistent_newer',amount.replace('40','50'),['UPDATE'],'amount',50,state=state,message_id='persistent-newer')
    run('persistent_stale_replay',amount,['NO_CHANGE'],'amount',50,state=state,input_path=first/'request.eml')
    # Require zero commits and no revision increment on both cached replays.
    for check in checks:
        if check['case'] in {'persistent_repeat','persistent_stale_replay'}:
            state_info=check['state']
            check['passed'] &= state_info['committed_changes']==0 and state_info['idempotent_replays']==1 and state_info['revision_before']==state_info['revision_after']
    result={'dataset':'Explicitly synthetic EML and ledger records, not company samples',
        'scope':'Supported grammar, field patches, boundaries and persistent local replay',
        'checks':checks,'passed':all(c['passed'] for c in checks),
        'note':'Supported amendment grammar is compiled deterministically before the model; model-call counts are reported separately. Local SQLite is a simulation, no finance system is modified.'}
    save(root/'validation.json',result)
    save(root.parent/'latest.json',{'report':str(root/'validation.json'),'passed':result['passed']})
    print('REPORT',root/'validation.json',flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
