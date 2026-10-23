from copy import deepcopy
from email.message import EmailMessage
from decimal import Decimal
from billing_agent.parsers import Parser
from billing_agent.amendments import parse_amendment
from billing_agent.models import Charge, Extraction, Evidence
from billing_agent.normalization import normalize_extraction
from billing_agent.financial import enrich_parties
from billing_agent.engine import process_batch
from billing_agent.state import process_with_state


def bill(**fields):
    row={'id':'bill-1','document_info':{'document_type':'Invoice','reference_no':'DN-1001','document_date':'2026-01-01'},
        'transaction_party':{'issuer':'Alpha LLC','recipient':'Beta LLC'},
        'charge_details':{'charge_item':'Handling Charge','amount':30,'currency':'USD','payment_term':'PP'},
        'financial_attributes':{'accounting_entity':'Beta LLC','posting_type':'AP'},'protected_note':'keep exactly'}
    row['charge_details'].update(fields)
    return row


def parse(text,name='amendment.eml',message_id='test'):
    message=EmailMessage();message['Subject']='Synthetic amendment';message['Message-ID']=f'<{message_id}@example.invalid>'
    message.set_content(text)
    parser=Parser();parser.parse(message.as_bytes(),name)
    charges=[]
    for evidence in parser.evidence:
        if (evidence.structured or {}).get('kind')=='email_statement':
            charge=parse_amendment(evidence)
            if charge is not None:charges.append(charge)
    result=normalize_extraction(Extraction(charges=charges),parser.evidence)
    result=enrich_parties(result,parser.evidence,'Beta LLC')
    return result,parser.evidence


AMOUNT='请将账单 DN-1001 的 Handling Fee 金额改为 USD 40，其他不变。'
TERM='Please change bill DN-1001 Handling Fee payment term to CC; other fields unchanged.'
CURRENCY='Please change bill DN-1001 Handling Fee currency to EUR; leave amount unchanged.'


def test_chinese_amount_patch_preserves_all_other_fields():
    x,e=parse(AMOUNT);old=bill();before=deepcopy(old)
    row=process_batch(x.charges,e,[old],'Beta LLC')[0]
    assert row['operation']=='UPDATE'
    assert row['audit_trail']['proposed_changes']=={'amount':{'before':30,'after':Decimal('40')}}
    assert row['charge_details']==dict(old['charge_details'],amount=Decimal('40'))
    assert row['document_info']==old['document_info']
    assert row['transaction_party']==old['transaction_party']
    assert row['financial_attributes']==old['financial_attributes']
    assert row['protected_note']=='keep exactly'
    assert old==before
    assert row['audit_trail']['financial_assessment']['status']=='RESOLVED_INHERITED'


def test_term_only_does_not_require_restatement_of_amount_currency_or_parties():
    x,e=parse(TERM);row=process_batch(x.charges,e,[bill()],'Beta LLC')[0]
    assert row['operation']=='UPDATE'
    assert row['audit_trail']['proposed_changes']=={'payment_term':{'before':'PP','after':'CC'}}
    assert row['charge_details']['amount']==30
    assert row['charge_details']['currency']=='USD'


def test_explicit_currency_only_preserves_amount():
    x,e=parse(CURRENCY);row=process_batch(x.charges,e,[bill()],'Beta LLC')[0]
    assert row['operation']=='UPDATE'
    assert row['charge_details']['currency']=='EUR'
    assert row['charge_details']['amount']==30


def test_currency_next_to_amount_is_not_permission_to_change_currency():
    x,e=parse(AMOUNT.replace('USD','EUR'))
    row=process_batch(x.charges,e,[bill()],'Beta LLC')[0]
    assert row['operation']=='PENDING_REVIEW'
    assert row['charge_details']['currency']=='USD'


def test_ambiguous_and_cross_entity_targets_are_blocked():
    x,e=parse(AMOUNT)
    other=bill(amount=35);other['id']='bill-2'
    assert process_batch(x.charges,e,[bill(),other],'Beta LLC')[0]['operation']=='PENDING_REVIEW'
    outside=bill();outside['financial_attributes']['accounting_entity']='Other LLC'
    row=process_batch(x.charges,e,[outside],'Beta LLC')[0]
    assert row['operation']=='PENDING_REVIEW'
    assert row['audit_trail']['proposed_changes']=={}


def test_no_target_never_becomes_create_and_reference_does_not_fall_back():
    x,e=parse(AMOUNT.replace('DN-1001','DN-9999'))
    x.charges[0].bill_of_lading='BL1'
    old=bill();old['transport_info']={'bill_of_lading':'BL1'}
    row=process_batch(x.charges,e,[old],'Beta LLC')[0]
    assert row['operation']=='PENDING_REVIEW'
    assert not row['audit_trail']['matched_bill_ids']


def test_negation_condition_clear_and_unsupported_fields_are_reviewed():
    lines=[
        'Do not change bill DN-1001 Handling Fee amount to USD 40.',
        'If approved, change bill DN-1001 Handling Fee amount to USD 40.',
        'Please change bill DN-1001 Handling Fee payment term to null.',
        'Please change bill DN-1001 Handling Fee amount to USD 40 and delete issuer.',
    ]
    for line in lines:
        x,e=parse(line)
        row=process_batch(x.charges,e,[bill()],'Beta LLC')[0]
        assert row['operation']=='PENDING_REVIEW',line


def test_forged_explicit_field_is_not_authorization():
    x,e=parse(AMOUNT)
    x.charges[0].explicit_changes.append('currency')
    row=process_batch(x.charges,e,[bill()],'Beta LLC')[0]
    assert row['operation']=='PENDING_REVIEW'
    assert row['audit_trail']['proposed_changes']=={}


def test_quoted_old_instruction_is_not_executed():
    x,e=parse('No changes requested.\n> '+AMOUNT)
    assert not x.charges
    x,e=parse('No changes requested.\n-----Original Message-----\n'+AMOUNT)
    assert not x.charges


def test_same_batch_duplicate_instruction_has_no_second_patch():
    x,e=parse(AMOUNT)
    rows=process_batch(x.charges+x.charges,e,[bill()],'Beta LLC')
    assert [r['operation'] for r in rows]==['UPDATE','NO_CHANGE']
    assert rows[1]['audit_trail']['proposed_changes']=={}


def test_conflicting_instructions_same_bill_are_both_blocked():
    x,e=parse(AMOUNT+'\n'+AMOUNT.replace('40','50'))
    rows=process_batch(x.charges,e,[bill()],'Beta LLC')
    assert [r['operation'] for r in rows]==['PENDING_REVIEW','PENDING_REVIEW']


def test_persistent_repeat_and_stale_replay_never_roll_back_later_update(tmp_path):
    db=tmp_path/'ledger.sqlite'
    x,e=parse(AMOUNT)
    rows,first=process_with_state(x,e,[bill()],'Beta LLC',{},db)
    assert rows[0]['operation']=='UPDATE'
    assert first['committed_changes']==1
    rows,second=process_with_state(x,e,[bill()],'Beta LLC',{},db)
    assert rows[0]['operation']=='NO_CHANGE'
    assert second['committed_changes']==0
    newer,ne=parse(AMOUNT.replace('40','50'),message_id='new')
    rows,new=process_with_state(newer,ne,[bill()],'Beta LLC',{},db)
    assert rows[0]['operation']=='UPDATE'
    assert new['ledger'][0]['charge_details']['amount']==50
    rows,last=process_with_state(x,e,[bill()],'Beta LLC',{},db)
    assert rows[0]['operation']=='NO_CHANGE'
    assert rows[0]['charge_details']['amount']==50
    assert last['ledger'][0]['charge_details']['amount']==50
    assert last['revision_before']==last['revision_after']


def test_rename_same_email_does_not_change_request_identity(tmp_path):
    db=tmp_path/'ledger.sqlite'
    x,e=parse(AMOUNT,name='first.eml')
    process_with_state(x,e,[bill()],'Beta LLC',{},db)
    x,e=parse(AMOUNT,name='renamed.eml')
    rows,report=process_with_state(x,e,[bill()],'Beta LLC',{},db)
    assert rows[0]['operation']=='NO_CHANGE'
    assert report['idempotent_replays']==1


def test_pending_review_is_never_applied_to_local_state(tmp_path):
    x,e=parse(AMOUNT)
    duplicate=bill(amount=35);duplicate['id']='bill-2'
    rows,report=process_with_state(x,e,[bill(),duplicate],'Beta LLC',{},tmp_path/'ledger.sqlite')
    assert rows[0]['operation']=='PENDING_REVIEW'
    assert report['committed_changes']==0
    assert [r['charge_details']['amount'] for r in report['ledger']]==[30,35]


def test_cached_json_decimal_representation_keeps_same_request_identity(tmp_path):
    from billing_agent.requests import request_key
    x,e=parse(AMOUNT)
    before=request_key(x.charges[0],e,'Beta LLC')
    x.charges[0].amount=Decimal('40.0')
    assert request_key(x.charges[0],e,'Beta LLC')==before


def test_persistent_create_replay_does_not_add_second_bill(tmp_path):
    ev=[Evidence(id='new',source='new.txt',location='text',
        text='INVOICE DN-NEW\nISSUED BY: Alpha LLC\nBILL TO: Beta LLC\nHandling Charge USD 25')]
    x=Extraction(charges=[Charge(reference_no='DN-NEW',charge_item='Handling Charge',amount=25,
        currency='USD',amount_quote='USD 25',evidence_ids=['new'])])
    db=tmp_path/'create.sqlite'
    rows,first=process_with_state(x,ev,[],'Beta LLC',{},db)
    assert rows[0]['operation']=='CREATE'
    rows,last=process_with_state(x,ev,[],'Beta LLC',{},db)
    assert rows[0]['operation']=='NO_CHANGE'
    assert len(last['ledger'])==1
    assert last['revision_before']==last['revision_after']


def test_concurrent_replay_commits_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    x,e=parse(AMOUNT)
    db=tmp_path/'concurrent.sqlite'
    def execute():
        return process_with_state(x,e,[bill()],'Beta LLC',{},db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:execute(),range(2)))
    assert sorted(rows[0]['operation'] for rows,report in results)==['NO_CHANGE','UPDATE']
    assert sum(report['committed_changes'] for rows,report in results)==1


def test_explicit_multi_field_patch_changes_only_those_fields():
    x,e=parse('Please change bill DN-1001 Handling Fee amount to USD 40 and payment term to CC.')
    row=process_batch(x.charges,e,[bill()],'Beta LLC')[0]
    assert row['operation']=='UPDATE'
    assert set(row['audit_trail']['proposed_changes'])=={'amount','payment_term'}
    assert row['charge_details']['amount']==40
    assert row['charge_details']['payment_term']=='CC'
    assert row['charge_details']['currency']=='USD'


def test_forged_charge_target_cannot_update_another_fee_on_same_bill():
    x,e=parse(AMOUNT)
    x.charges[0].charge_item='Doc Fee'
    other=bill();other['charge_details']['charge_item']='Documentation Fee'
    row=process_batch(x.charges,e,[other],'Beta LLC')[0]
    assert row['operation']=='PENDING_REVIEW'
    assert row['audit_trail']['proposed_changes']=={}


def test_incoming_parties_cannot_silently_reverse_existing_posting():
    x,e=parse('INVOICE\nISSUED BY: Beta LLC\nBILL TO: Alpha LLC\n'+AMOUNT)
    row=process_batch(x.charges,e,[bill()],'Beta LLC')[0]
    assert row['operation']=='PENDING_REVIEW'
    assert row['transaction_party']==bill()['transaction_party']
    assert row['financial_attributes']['posting_type']=='AP'
    assert any('conflicts with the existing bill' in reason for reason in row['audit_trail']['review_reasons'])
