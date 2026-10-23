from decimal import Decimal
from billing_agent.models import Charge, Evidence, Extraction, DocumentTotal
from billing_agent.normalization import normalize_extraction, is_summary_label


def evidence(text, identity='e1', source='one.pdf'):
    return Evidence(id=identity, source=source, location='page:1', text=text)


def charge(name='Handling Charge', **kwargs):
    return Charge(charge_item=name, amount=Decimal('65'), amount_quote='65.00',
                  evidence_ids=['e1'], **kwargs)


def test_summary_removed_but_audited_and_same_name_rows_preserved():
    data = Extraction(charges=[charge(), charge(), charge('TOTAL'), charge('Grand Total:')])
    result = normalize_extraction(data, [evidence('Currency USD\nHandling Charge 65.00')])
    assert len(result.charges) == 2
    assert len(result.excluded_candidates) == 2
    assert len(data.charges) == 4  # Original model artifact remains unchanged.


def test_legitimate_fee_containing_total_is_not_filtered():
    assert not is_summary_label('Total Logistics Service Fee')
    assert not is_summary_label('Total Handling Charge')
    assert is_summary_label('SUBTOTAL')
    assert is_summary_label('合计')


def test_currency_filled_only_from_supported_linked_source():
    result = normalize_extraction(Extraction(charges=[charge()]), [evidence('CURRENCY USD\nHandling Charge 65.00')])
    assert result.charges[0].currency == 'USD'
    assert result.normalization_events[0]['evidence'][0]['quote'] == 'USD'


def test_weight_kgs_does_not_compete_with_currency_but_explicit_kgs_does():
    source = [evidence('CURRENCY USD\nWeight 15800 KGS\nHandling Charge 65.00')]
    assert normalize_extraction(Extraction(charges=[charge()]), source).charges[0].currency == 'USD'
    source = [evidence('CURRENCY KGS\nHandling Charge 65.00')]
    assert normalize_extraction(Extraction(charges=[charge()]), source).charges[0].currency == 'KGS'


def test_currency_does_not_cross_files_or_infer_from_symbol():
    source = [evidence('H/C $65.00'), evidence('Currency USD', 'other', 'two.pdf')]
    assert normalize_extraction(Extraction(charges=[charge()]), source).charges[0].currency is None
    invented = normalize_extraction(Extraction(charges=[charge(currency='USD')]), source)
    assert invented.charges[0].currency is None
    assert invented.normalization_events[0]['rule'] == 'discard_currency_without_source_code'


def test_multi_currency_missing_quote_or_invalid_ref_remains_null():
    for row, source in [
        (charge(), [evidence('USD CNY Handling Charge 65.00')]),
        (charge(), [evidence('USD Handling Charge 35.00')]),
        (charge(), [evidence('USD 65.00', 'other')]),
    ]:
        assert normalize_extraction(Extraction(charges=[row]), source).charges[0].currency is None


def test_currency_lowercase_is_normalized():
    result = normalize_extraction(Extraction(charges=[charge(currency='usd')]), [evidence('USD 65.00')])
    assert result.charges[0].currency == 'USD'


def test_paid_amount_and_balance_not_compared_to_gross_lines():
    totals = [DocumentTotal(reference_no='DN1', currency='USD', amount=value,
        evidence_id='e1', quote=quote) for value,quote in [
            ('100', 'TOTAL 100'), ('20', 'PAID AMOUNT 20'), ('80', 'GRAND TOTAL BALANCE DUE USD 80')]]
    result = normalize_extraction(Extraction(totals=totals), [])
    assert [t.amount for t in result.totals] == [Decimal('100')]
    assert len(result.excluded_candidates) == 2
