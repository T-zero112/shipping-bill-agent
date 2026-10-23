from copy import deepcopy
import pytest
from billing_agent.models import Charge,Evidence,Extraction
from billing_agent.business import enrich_business,validate_business
from billing_agent.financial import enrich_parties
from billing_agent.engine import process,bill_overlap
from billing_agent.linking import linked_scope,association


def page(n,text,source='batch.pdf'):
    return Evidence(id=f'{source}-{n}',source=source,location=f'page:{n}',text=text)


def fixture(ref2='INV1001',source2='batch.pdf',extra=''):
    ev=[page(1,'Invoice No: INV1001\nInvoice Date: 2026-10-22\nISSUER: Alpha\nBILL TO: Beta\n'
             'MASTER B/L NO: MASTER123\nHOUSE B/L NO: HOUSE123\nCNTR. NO.: ABCD1234567, EFGH7654321'+extra),
        page(2,f'Invoice No: {ref2}\nHandling Fee USD 40',source2)]
    c=Charge(charge_item='Handling Fee',amount=40,currency='USD',evidence_ids=[ev[1].id],amount_quote='USD 40')
    ext=enrich_business(Extraction(charges=[c]),ev)
    ext=enrich_parties(ext,ev,'Beta')
    return ext.charges[0],ev


def test_same_explicit_document_shares_date_parties_and_typed_identifiers():
    c,ev=fixture()
    assert c.document_date=='2026-10-22' and c.reference_no=='INV1001'
    assert c.issuer=='Alpha' and c.recipient=='Beta'
    assert not validate_business(c,ev)
    r=process(c,ev,[],'Beta')
    assert r['operation']=='CREATE' and r['financial_attributes']['posting_type']=='AP'
    assert r['transport_info']['master_bills']==['MASTER123']
    assert r['transport_info']['house_bills']==['HOUSE123']
    assert r['transport_info']['containers']==['ABCD1234567','EFGH7654321']
    assert r['transport_info']['bill_of_lading'] is None and r['transport_info']['container_no'] is None
    assert ev[0].id in r['audit_trail']['document_association']['evidence_ids']


@pytest.mark.parametrize('ref,source',[('OTHER1001','batch.pdf'),('INV1001','another.pdf'),('', 'batch.pdf')])
def test_different_reference_source_or_unlabelled_page_never_borrows(ref,source):
    c,ev=fixture(ref,source)
    assert c.document_date is None and c.issuer is None and not c.transport_identifiers
    assert ev[0].id not in [e.id for e in linked_scope(c,ev)]


@pytest.mark.parametrize('extra',['\nInvoice Date: 2026-10-23','\nISSUER: Other','\nInvoice No: OTHER1001','\nCREDIT NOTE'])
def test_conflicting_context_does_not_link(extra):
    c,ev=fixture(extra=extra)
    assert c.issuer is None and c.document_date is None
    assert process(c,ev,[],'Beta')['operation']=='PENDING_REVIEW'


def test_master_never_matches_explicit_house_with_same_value():
    c,ev=fixture()
    c.bill_of_lading=None
    c.transport_identifiers=[f for f in c.transport_identifiers if f['kind']=='master_bl']
    assert not bill_overlap(c,{'house_bills':['MASTER123'],'bill_of_lading':'MASTER123'})
    assert bill_overlap(c,{'master_bills':['MASTER123']})
    assert bill_overlap(c,{'bill_of_lading':'MASTER123'})


def test_forged_link_identifiers_cannot_reach_matching():
    c,ev=fixture('OTHER1001')
    c.transport_identifiers=[{'kind':'master_bl','value':'MASTER123','evidence_id':ev[0].id,'quote':'MASTER B/L NO: MASTER123'}]
    old={'id':'old','charge_details':{'charge_item':'Handling Fee','currency':'USD','amount':40},
         'transport_info':{'master_bills':['MASTER123']},'financial_attributes':{'accounting_entity':'Beta'}}
    r=process(c,ev,[old],'Beta')
    assert not r['audit_trail']['matched_bill_ids'] and r['operation']=='PENDING_REVIEW'


def test_multiple_containers_require_exact_set_for_container_tier():
    c,ev=fixture()
    c.reference_no=None;c.bill_of_lading=None
    c.transport_identifiers=[f for f in c.transport_identifiers if f['kind']=='container']
    old={'id':'old','document_info':{'document_date':'2026-10-22'},'transaction_party':{'issuer':'Alpha','recipient':'Beta'},
         'charge_details':{'charge_item':'Handling Fee','currency':'USD','amount':40},
         'financial_attributes':{'accounting_entity':'Beta','posting_type':'AP'},'transport_info':{'container_no':'ABCD1234567'}}
    assert not process(c,ev,[old],'Beta')['audit_trail']['matched_bill_ids']
    old['transport_info']={'containers':['EFGH7654321','ABCD1234567']}
    assert process(c,ev,[old],'Beta')['audit_trail']['matched_bill_ids']==['old']


def test_no_cross_page_amount_or_quantity_borrowing():
    c,ev=fixture(extra='\nQuantity: 99\nUnit Price: 999')
    assert c.quantity is None and c.unit_price is None and c.amount==40


def test_model_reference_cannot_authorize_link():
    c,ev=fixture('OTHER1001')
    c.reference_no='INV1001'
    assert len(linked_scope(c,ev))==1
    assert association(c,ev)['review_reasons']


def test_explicit_row_container_overrides_document_container_list():
    c,ev=fixture()
    row=Evidence(id='fee-row',source='batch.pdf',location='page:2/charge-row:1',parent_id=ev[1].id,
                 text='Handling Fee USD 40 Container No: ABCD1234567')
    ev.append(row)
    c.evidence_ids=['fee-row'];c.transport_identifiers=[]
    c=enrich_business(Extraction(charges=[c]),ev).charges[0]
    assert {f['value'] for f in c.transport_identifiers if f['kind']=='container'}=={'ABCD1234567'}
    c.transport_identifiers.append({'kind':'container','value':'EFGH7654321','evidence_id':ev[0].id,
                                  'quote':'CNTR. NO.: ABCD1234567, EFGH7654321'})
    from billing_agent.linking import validate_transport
    assert validate_transport(c,ev)


def test_currency_header_can_cross_verified_pages_but_not_documents():
    from billing_agent.normalization import normalize_extraction
    for same in [True,False]:
        c,ev=fixture('INV1001' if same else 'OTHER1001',extra='\nCURRENCY: USD')
        ev[1].text=ev[1].text.replace('USD 40','40')
        c.amount_quote='40';c.currency=None
        c=normalize_extraction(Extraction(charges=[c]),ev).charges[0]
        assert (c.currency=='USD') if same else c.currency is None


def test_shared_master_does_not_override_conflicting_house():
    c,_=fixture()
    assert not bill_overlap(c,{'master_bills':['MASTER123'],'house_bills':['OTHER123']})


def test_multiple_same_role_bills_are_not_matched_by_partial_overlap():
    c,ev=fixture(extra='\nMASTER B/L NO: OTHER123')
    assert not bill_overlap(c,{'master_bills':['MASTER123']})
    assert bill_overlap(c,{'master_bills':['MASTER123','OTHER123']})
