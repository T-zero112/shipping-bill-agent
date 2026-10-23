from copy import deepcopy
from decimal import Decimal
import pytest
from billing_agent.models import Evidence,Charge,Extraction
from billing_agent.normalization import normalize_extraction
from billing_agent.financial import enrich_parties
from billing_agent.engine import process,process_batch
from billing_agent.contracts import validate_results,OutputContractError

TEXT='''CREDIT NOTE No: CN-1001
Original Invoice No: INV-1001
ISSUER: Alpha LLC
BILL TO: Beta LLC
Handling Fee USD 30'''


def fixture(text=TEXT,entity='Beta LLC'):
    ev=[Evidence(id='e',source='synthetic.txt',location='text',text=text)]
    amount=Decimal('-30') if 'USD -30' in text else Decimal('30')
    c=Charge(document_type='CREDIT NOTE',reference_no='CN-1001',charge_item='Handling Fee',
             amount=amount,currency='USD',amount_quote='USD '+str(amount),evidence_ids=['e'])
    ext=enrich_parties(normalize_extraction(Extraction(charges=[c]),ev),ev,entity)
    old={'id':'original-1','document_info':{'document_type':'INVOICE','reference_no':'INV-1001'},
         'transaction_party':{'issuer':'Alpha LLC','recipient':'Beta LLC'},
         'charge_details':{'charge_item':'Handling Charge','amount':40,'currency':'USD'},
         'financial_attributes':{'accounting_entity':entity,'posting_type':'AP' if entity=='Beta LLC' else 'AR'}}
    return ext.charges[0],ev,old


@pytest.mark.parametrize('entity,posting',[('Alpha LLC','AR'),('Beta LLC','AP')])
@pytest.mark.parametrize('negative',[False,True])
def test_credit_keeps_original_direction_and_creates_negative_record(entity,posting,negative):
    c,ev,old=fixture(TEXT.replace('USD 30','USD -30') if negative else TEXT,entity)
    before=deepcopy(old);r=process(c,ev,[old],entity)
    assert r['operation']=='CREATE' and r['financial_attributes']['posting_type']==posting
    assert r['charge_details']['amount']==-30 and r['credit_adjustment']['remaining_after']==10
    assert r['credit_adjustment']['original_bill_id']=='original-1' and old==before
    validate_results([r])


@pytest.mark.parametrize('change',[
    'missing_reference','ambiguous_reference','missing_party','reversed_parties','wrong_currency',
    'wrong_entity','wrong_posting','too_large','ambiguous_original','missing_original','draft','paid_invoice'])
def test_uncertain_credits_remain_review(change):
    text=TEXT
    if change=='missing_reference':text=text.replace('Original Invoice No: INV-1001','')
    if change=='ambiguous_reference':text+='\nOriginal Invoice No: OTHER-1001'
    if change=='missing_party':text=text.replace('BILL TO: Beta LLC','')
    if change=='reversed_parties':text=text.replace('ISSUER: Alpha LLC','ISSUER: Beta LLC').replace('BILL TO: Beta LLC','BILL TO: Alpha LLC')
    if change=='draft':text+='\nDRAFT - SUBJECT TO APPROVAL'
    c,ev,old=fixture(text)
    if change=='wrong_currency':old['charge_details']['currency']='EUR'
    if change=='wrong_entity':old['financial_attributes']['accounting_entity']='Other LLC'
    if change=='wrong_posting':old['financial_attributes']['posting_type']='AR'
    if change=='too_large':old['charge_details']['amount']=20
    if change=='paid_invoice':old['settlement']={'paid_amount':40,'outstanding_amount':0}
    existing=[] if change=='missing_original' else [old,dict(old,id='other')] if change=='ambiguous_original' else [old]
    r=process(c,ev,existing,'Beta LLC')
    assert r['operation']=='PENDING_REVIEW' and r['financial_attributes']['posting_type'] is None
    assert r['audit_trail']['review_reasons']
    validate_results([r])


def test_prior_and_batch_credits_cannot_exceed_original_amount():
    c,ev,old=fixture()
    first=process(c,ev,[old],'Beta LLC');first.update(id='credit-1')
    next_c,next_ev,_=fixture(TEXT.replace('CN-1001','CN-1002'))
    next_c.reference_no='CN-1002'
    next_ev[0].id='e2';next_ev[0].source='second.txt';next_c.evidence_ids=['e2']
    assert process(next_c,next_ev,[old,first],'Beta LLC')['operation']=='PENDING_REVIEW'
    rows=process_batch([c,next_c],ev+next_ev,[old],'Beta LLC')
    assert all(r['operation']=='PENDING_REVIEW' for r in rows)
    assert all(any('Batch credit aggregate' in reason for reason in r['audit_trail']['review_reasons']) for r in rows)


def test_repeat_credit_is_no_change_and_modified_repeat_is_review():
    c,ev,old=fixture();r=process(c,ev,[old],'Beta LLC');r['id']='credit-1'
    assert process(c,ev,[old,r],'Beta LLC')['operation']=='NO_CHANGE'
    r['charge_details']['amount']=-20
    assert process(c,ev,[old,r],'Beta LLC')['operation']=='PENDING_REVIEW'


def test_same_reference_in_another_file_cannot_supply_original_link():
    c,ev,old=fixture(TEXT.replace('Original Invoice No: INV-1001',''))
    ev.append(Evidence(id='other',source='other.txt',location='text',text=TEXT))
    assert process(c,ev,[old],'Beta LLC')['operation']=='PENDING_REVIEW'


def test_state_replay_preserves_original_and_adds_one_credit(tmp_path):
    from billing_agent.state import process_with_state
    c,ev,old=fixture();path=tmp_path/'ledger.sqlite'
    rows,state=process_with_state(Extraction(charges=[c]),ev,[old],'Beta LLC',{},path)
    assert state['committed_changes']==1 and len(state['ledger'])==2
    assert state['ledger'][0]==old
    rows,state=process_with_state(Extraction(charges=[c]),ev,[old],'Beta LLC',{},path)
    assert state['committed_changes']==0 and rows[0]['operation']=='NO_CHANGE'


def test_credit_contract_rejects_positive_adjustment():
    c,ev,old=fixture();r=process(c,ev,[old],'Beta LLC')
    r['charge_details']['amount']=30
    with pytest.raises(OutputContractError):validate_results([r])


@pytest.mark.parametrize('amount',['NaN','Infinity','bad',-40])
def test_invalid_original_amount_stays_review_with_legal_json(amount):
    c,ev,old=fixture();old['charge_details']['amount']=amount
    r=process(c,ev,[old],'Beta LLC')
    assert r['operation']=='PENDING_REVIEW'
    validate_results([r])


def test_confirmed_aliases_support_credit_parties_without_guessing():
    c,ev,old=fixture(TEXT.replace('ISSUER: Alpha LLC','ISSUER: ALPHA'))
    r=process(c,ev,[old],'Beta LLC',{'Alpha LLC':['ALPHA']})
    assert r['operation']=='CREATE' and r['financial_attributes']['posting_type']=='AP'


def test_negative_credit_quantity_and_total_use_credit_magnitude():
    from billing_agent.models import DocumentTotal
    c,ev,old=fixture(TEXT.replace('USD 30','USD -30')+'\nQuantity: 1 Unit Price: 30\nTotal credit USD 30')
    total=DocumentTotal(reference_no='CN-1001',currency='USD',amount=30,evidence_id='e',quote='Total credit USD 30')
    r=process_batch([c],ev,[old],'Beta LLC',[total])[0]
    assert r['operation']=='CREATE' and r['charge_details']['amount']==-30


def test_original_reference_does_not_make_credit_page_multiple_documents():
    from billing_agent.linking import association
    c,ev,old=fixture();ev[0].location='page:1'
    assert not association(c,ev)['review_reasons']
