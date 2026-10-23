from copy import deepcopy
from decimal import Decimal
import pytest
from billing_agent.models import Charge, Evidence, Extraction
from billing_agent.business import enrich_business, validate_business, date_value
from billing_agent.engine import process


TEXT = '''A charges B. Handling Fee USD 40
Invoice Date: 2026-10-21
Quantity: 2 Unit Price: 20
Payment Terms: PP
B/L No: BL12345
Booking No: BK12345
Container No: ABCD1234567
Subtotal: USD 40
Tax: USD 4
Total Amount: USD 44'''


def fixture(text=TEXT):
    evidence = [Evidence(id='e', source='synthetic.txt', location='row', text=text)]
    c = Charge(charge_item='Handling Fee', amount=40, currency='USD', evidence_ids=['e'],
               amount_quote='USD 40', issuer='A', recipient='B',
               direction='ISSUER_CHARGES_RECIPIENT', direction_quote='A charges B.')
    c = enrich_business(Extraction(charges=[c]), evidence).charges[0]
    return c, evidence


def test_all_fields_are_extracted_with_exact_proofs_and_serialized():
    c, ev = fixture()
    assert validate_business(c, ev) == []
    assert c.document_date == '2026-10-21'
    assert (c.quantity, c.unit_price, c.subtotal, c.tax, c.document_total) == (2,20,40,4,44)
    assert c.payment_term == 'PP'
    result = process(c, ev, [], 'B')
    assert result['operation'] == 'CREATE'
    assert result['transport_info']['booking_no'] == 'BK12345'
    assert result['document_amounts']['tax'] == 4


@pytest.mark.parametrize('tier', [1,2,3,4])
def test_extracted_fields_match_old_ledger_at_each_tier(tier):
    c, ev = fixture()
    if tier == 1:
        c.reference_no = 'INV123'
    if tier >= 3:
        c.bill_of_lading = None
        c.transport_identifiers=[f for f in c.transport_identifiers if f['kind']=='container']
    if tier == 4:
        c.booking_no = None
    old = {'id':'old-1','document_info':{'reference_no':'INV123' if tier==1 else 'OTHER', 'document_date':c.document_date},
           'transaction_party':{'issuer':'A','recipient':'B'},
           'transport_info':{'bill_of_lading':'BL12345', 'booking_no':'BK12345', 'container_no':'ABCD1234567'},
           'charge_details':{'charge_item':'Handling Charge','amount':40,'currency':'USD','payment_term':'PP',
                             'quantity':2,'unit_price':20},
           'financial_attributes':{'accounting_entity':'B','posting_type':'AP'}}
    before = deepcopy(old)
    result = process(c, ev, [old], 'B')
    assert result['operation'] == 'NO_CHANGE'
    assert result['audit_trail']['matched_bill_ids'] == ['old-1']
    assert old == before
    ambiguous = process(c, ev, [old,dict(old,id='old-2')], 'B')
    assert ambiguous['operation'] == 'PENDING_REVIEW'


def test_forged_identifier_is_removed_before_matching():
    c, ev = fixture()
    c.bill_of_lading = 'FORGED123'
    c.booking_no = c.container_no = None
    old = {'id':'victim','charge_details':{'charge_item':'Handling Fee','currency':'USD','amount':40},
           'transport_info':{'bill_of_lading':'FORGED123'},
           'financial_attributes':{'accounting_entity':'B'}}
    r = process(c,ev,[old],'B')
    assert r['operation'] == 'PENDING_REVIEW'
    assert not r['audit_trail']['matched_bill_ids']
    assert r['transport_info']['bill_of_lading'] is None


def test_cross_page_or_cross_file_evidence_cannot_fill_fields():
    c, ev = fixture('A charges B. Handling Fee USD 40')
    ev += [Evidence(id='other',source='other.pdf',location='page:1',text=TEXT)]
    c.business_evidence['booking_no'] = {'evidence_id':'other','quote':'Booking No: BK12345'}
    c.booking_no = 'BK12345'
    assert validate_business(c,ev)
    assert c.booking_no is None


def test_ambiguous_dates_identifiers_and_container_lists_stay_null():
    c, ev = fixture(TEXT.replace('2026-10-21','10/03/2025') + '\nB/L No: OTHER123')
    assert c.document_date is None and c.bill_of_lading is None
    c, _ = fixture(TEXT.replace('ABCD1234567','ABCD1234567, EFGH1234567'))
    assert c.container_no is None
    assert date_value('10-24-2026') == '2026-10-24'
    assert date_value('2026-02-30') is None


def test_amount_relationships_and_negative_values_block():
    for text in [TEXT.replace('Quantity: 2','Quantity: 3'), TEXT.replace('Tax: USD 4','Tax: USD 5'),
                 TEXT.replace('Unit Price: 20','Unit Price: -20')]:
        c, ev = fixture(text)
        assert process(c,ev,[],'B')['operation'] == 'PENDING_REVIEW'


def test_labelled_numbers_do_not_pick_up_weights_or_free_text():
    c, ev = fixture('A charges B. Handling Fee USD 40\nWEIGHT 1000 KGS\nCOD 30 DAYS\n2026-10-21')
    assert not c.business_evidence


def test_modification_context_does_not_expand_patch_fields():
    c, ev = fixture()
    c.intent = 'MODIFY'
    c.business_evidence.clear()
    c.quantity = None
    enrich_business(Extraction(charges=[c]),ev)
    assert c.quantity is None


def test_tax_percentage_is_not_tax_amount():
    c, _ = fixture(TEXT.replace('Tax: USD 4', 'Tax: 10%'))
    assert c.tax is None


def test_other_currency_summary_is_not_borrowed():
    c, _ = fixture(TEXT.replace('Tax: USD 4','Tax: EUR 4'))
    assert c.tax is None


def test_taxed_document_line_sum_and_source_isolation():
    from billing_agent.engine import process_batch
    from billing_agent.models import DocumentTotal
    c, ev = fixture()
    c.reference_no = 'INV123'
    total = DocumentTotal(reference_no='INV123',currency='USD',amount=44,evidence_id='e',quote='Total Amount: USD 44')
    assert process_batch([c],ev,[],'B',[total])[0]['operation'] == 'CREATE'
    other = c.model_copy(deep=True)
    other.evidence_ids = ['other']
    other.business_evidence = {f:dict(p,evidence_id='other') for f,p in other.business_evidence.items()}
    other.transport_identifiers=[dict(p,evidence_id='other') for p in other.transport_identifiers]
    ev.append(Evidence(id='other',source='another.txt',location='row',text=TEXT))
    assert all(r['operation']=='CREATE' for r in process_batch([c,other],ev,[],'B',[total]))


def test_entity_scope_is_applied_before_matching_tiers():
    c, ev = fixture()
    c.reference_no = 'INV123'
    foreign = {'id':'foreign','document_info':{'reference_no':'INV123'},
               'charge_details':{'charge_item':'Handling Charge','amount':40,'currency':'USD'},
               'financial_attributes':{'accounting_entity':'C'}}
    own = deepcopy(foreign)
    own.update(id='own',document_info={'reference_no':'OTHER'},transport_info={'bill_of_lading':'BL12345'})
    own['financial_attributes']={'accounting_entity':'B'}
    result = process(c,ev,[foreign,own],'B')
    assert result['audit_trail']['matched_bill_ids']==['own']


def test_parent_metadata_applies_but_quantity_stays_in_its_own_row():
    row = Evidence(id='row',source='bill.pdf',location='page:1/row:1',parent_id='page',text='Handling Fee USD 40')
    page = Evidence(id='page',source='bill.pdf',location='page:1',text='Invoice Date: 2026-10-21\nQuantity: 99\nBooking No: BK12345')
    c = Charge(charge_item='Handling Fee',amount=40,evidence_ids=['row'])
    enrich_business(Extraction(charges=[c]),[row,page])
    assert c.booking_no=='BK12345' and c.quantity is None
