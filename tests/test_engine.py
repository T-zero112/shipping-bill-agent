from copy import deepcopy
from decimal import Decimal
from billing_agent.models import Charge, Evidence, DocumentTotal
from billing_agent.engine import process, process_batch, numeric_quote_supports

TEXT = 'A charges B. DN1 Handling Fee USD 40. Change amount to 40 only.'
EV = [Evidence(id='e1', source='synthetic.txt', location='row:1', text=TEXT)]


def charge(**kwargs):
    values = dict(reference_no='DN1', issuer='A', recipient='B', charge_item='Handling Fee',
        amount=Decimal('40'), currency='USD', evidence_ids=['e1'], amount_quote='USD 40',
        direction='ISSUER_CHARGES_RECIPIENT', direction_quote='A charges B.')
    values.update(kwargs)
    return Charge(**values)


def old():
    return {'id':'bill-1', 'document_info':{'reference_no':'DN1', 'document_date':'2026-01-01'},
        'transaction_party':{'issuer':'A','recipient':'B'},
        'charge_details':{'charge_item':'Handling Charge', 'amount':30, 'currency':'USD','payment_term':'PP'},
        'financial_attributes':{'accounting_entity':'B','posting_type':'AP'}}


def test_create_and_entity_perspective():
    assert process(charge(), EV, [], 'B')['operation'] == 'CREATE'
    assert process(charge(), EV, [], 'B')['financial_attributes']['posting_type'] == 'AP'
    assert process(charge(), EV, [], 'A')['financial_attributes']['posting_type'] == 'AR'


def test_patch_preserves_old_fields_and_does_not_mutate():
    original = old()
    before = deepcopy(original)
    result = process(charge(intent='MODIFY', explicit_changes=['amount'], change_quote='Change amount to 40 only.'), EV, [original], 'B')
    assert result['operation'] == 'UPDATE'
    assert result['charge_details'] == dict(before['charge_details'], amount=Decimal('40'))
    assert result['document_info'] == before['document_info']
    assert original == before


def test_observation_is_not_change_authorization():
    assert process(charge(), EV, [old()], 'B')['operation'] == 'PENDING_REVIEW'


def test_same_name_multiple_candidates_block_update():
    assert process(charge(), EV, [old(),old()], 'B')['operation'] == 'PENDING_REVIEW'


def test_unchanged_replay_is_no_change():
    row = old(); row['charge_details']['amount'] = 40
    assert process(charge(), EV, [row], 'B')['operation'] == 'NO_CHANGE'


def test_fabricated_or_mismatched_amount_quote_blocks():
    for c in [charge(amount_quote='USD 99'), charge(amount=Decimal('41')), charge(evidence_ids=['missing'])]:
        assert process(c, EV, [], 'B')['operation'] == 'PENDING_REVIEW'


def test_unknown_entity_currency_and_conditional_charge_block():
    for c in [charge(currency='ZZZ'), charge(actual_charge=False), charge(direction='UNKNOWN')]:
        assert process(c, EV, [], 'B')['operation'] == 'PENDING_REVIEW'
    assert process(charge(), EV, [], 'C')['operation'] == 'PENDING_REVIEW'


def test_modify_without_target_never_creates():
    assert process(charge(intent='MODIFY'), EV, [], 'B')['operation'] == 'PENDING_REVIEW'


def test_duplicate_claim_is_flagged_without_losing_possible_rows():
    ev2 = Evidence(id='e2', source='synthetic.txt', location='row:2', text=TEXT)
    results = process_batch([charge(),charge(),charge(evidence_ids=['e2'])], EV+[ev2], [], 'B')
    assert len(results) == 3
    assert results[0]['operation'] == results[1]['operation'] == 'PENDING_REVIEW'
    assert results[2]['operation'] == 'CREATE'


def test_declared_total_mismatch_blocks():
    evidence = EV + [Evidence(id='total', source='synthetic.txt', location='footer', text='Total USD 100')]
    total = DocumentTotal(reference_no='DN1', currency='USD', amount=Decimal('100'), evidence_id='total', quote='Total USD 100')
    result = process_batch([charge()], evidence, [], 'B', [total])[0]
    assert result['operation'] == 'PENDING_REVIEW'
    assert 'Document total unsupported or line sum mismatch' in result['audit_trail']['review_reasons']


def test_local_pdf_cannot_automatically_post():
    ev = [Evidence(id='e1', source='bill.pdf', location='page:1', text=TEXT, method='local_pdf_unverified')]
    assert process(charge(), ev, [], 'B')['operation'] == 'PENDING_REVIEW'


def test_credit_note_requires_adjustment_policy():
    assert process(charge(document_type='Credit Note'), EV, [], 'B')['operation'] == 'PENDING_REVIEW'


def test_pdf_currency_adjacent_to_amount_is_supported():
    assert numeric_quote_supports('245.0000USD', Decimal('245'))


def test_model_amount_thousands_separator_normalized():
    assert charge(amount='1,410.40').amount == Decimal('1410.40')
