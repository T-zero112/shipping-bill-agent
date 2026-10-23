from decimal import Decimal
from billing_agent.models import Charge, Evidence, Extraction
from billing_agent.normalization import normalize_extraction


def statement(identity, text, source='mail.eml', parent='body'):
    return Evidence(id=identity, source=source, parent_id=parent, location='body:line:'+identity,
                    text=text, structured={'kind':'email_statement'})


def test_email_name_and_amount_bind_to_pay_line_free_line_only_conflict():
    fee = statement('paid','SHA pay TAR: H/C $50')
    free = statement('free','Carrier Free H/C')
    charge = Charge(charge_item='Carrier Free H/C', amount=50, currency='USD',
                    amount_quote='Carrier Free H/C', evidence_ids=['paid'])
    result = normalize_extraction(Extraction(charges=[charge]), [fee,free]).charges[0]
    assert result.charge_item == result.source_description == result.name_quote == 'H/C'
    assert result.amount_quote == fee.text
    assert result.evidence_ids == ['paid']
    assert result.conflict_evidence_ids == ['free']
    assert result.currency is None
    assert result.warnings


def test_free_statement_does_not_supply_amount_or_repair_wrong_reference():
    fee = statement('paid','SHA pay TAR: H/C $50')
    free = statement('free','Carrier Free H/C')
    charge = Charge(charge_item='Carrier Free H/C', amount=50,
                    amount_quote=free.text, evidence_ids=['free'])
    result = normalize_extraction(Extraction(charges=[charge]), [fee,free]).charges[0]
    assert result.source_description is None
    assert result.name_quote is None
    assert result.evidence_ids == ['free']


def test_unrelated_free_service_or_other_email_does_not_conflict():
    fee = statement('paid','Doc Fee $20')
    unrelated = statement('free','Carrier Free H/C')
    other_email = statement('other','Free Doc Fee',source='different.eml')
    charge = Charge(charge_item='Doc Fee', amount=20, amount_quote=fee.text, evidence_ids=['paid'])
    result = normalize_extraction(Extraction(charges=[charge]), [fee,unrelated,other_email]).charges[0]
    assert not result.conflict_evidence_ids


def test_pdf_name_is_bound_by_columns_not_deleting_uppercase_or_digits():
    text = 'WB CODE9 4 EXTRA FREE TIME CHARGE USD 70'
    row = Evidence(id='row', source='a.pdf', location='page:1/charge-row:1', text=text,
        structured={'kind':'pdf_charge_row','description':'4 EXTRA FREE TIME CHARGE',
                    'charge_code':'CODE9','amount':'70'})
    charge = Charge(charge_item=text, amount=70, amount_quote=text,evidence_ids=['row'])
    result = normalize_extraction(Extraction(charges=[charge]), [row]).charges[0]
    assert result.charge_item == '4 EXTRA FREE TIME CHARGE'
    assert result.charge_code == 'CODE9'
    assert result.source_description in row.text


def test_pdf_wrong_amount_cannot_borrow_description_from_another_row():
    row = Evidence(id='row',source='a.pdf',location='row:1',text='THC 70',
        structured={'kind':'pdf_charge_row','description':'THC','charge_code':None,'amount':'70'})
    charge = Charge(charge_item='uncertain',amount=80,amount_quote='80',evidence_ids=['row'])
    result = normalize_extraction(Extraction(charges=[charge]), [row]).charges[0]
    assert result.charge_item == 'uncertain'

