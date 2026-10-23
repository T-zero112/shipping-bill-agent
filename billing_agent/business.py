"""Label-bound business facts; exact quotes stay in the charge's document scope."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

NUM = r'-?\d+(?:,\d{3})*(?:\.\d+)?'
FIELDS = ('document_date', 'quantity', 'unit_price', 'payment_term', 'bill_of_lading',
          'booking_no', 'container_no', 'subtotal', 'tax', 'document_total')
NUMERIC = {'quantity', 'unit_price', 'subtotal', 'tax', 'document_total'}
SUMMARY = {'subtotal', 'tax', 'document_total'}
LABELS = {
    'document_date': r'(?:D/C\s+DATE|INVOICE\s+DATE|ISSUE\s+DATE|DATE\s+OF\s+ISSUE|开票日期|账单日期)',
    'quantity': r'(?:QUANTITY|QTY|数量)',
    'unit_price': r'(?:UNIT\s+PRICE|单价)',
    'payment_term': r'(?:PAYMENT\s+TERMS?|FREIGHT\s+TERMS?|付款方式)',
    'bill_of_lading': r'(?:(?:MASTER\s+|HOUSE\s+|M\s*|H\s*)?B/L|BILL\s+OF\s+LADING|提单号)',
    'booking_no': r'(?:BOOKING|BKG|订舱号)',
    'container_no': r'(?:CONTAINER|CNTR\.?|箱号)',
    'subtotal': r'(?:SUB\s*TOTAL|小计)',
    'tax': r'(?:TAX\s+AMOUNT|VAT\s+AMOUNT|TAX|税额)',
    'document_total': r'(?:TOTAL\s+AMOUNT|GRAND\s+TOTAL|总计|合计)',
}


def date_value(raw):
    formats = ['%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d']
    # US vs day-first dates are accepted only when the numeric day disambiguates.
    m = re.fullmatch(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', raw)
    if m:
        a, b, _ = map(int, m.groups())
        if b > 12 or a == b:
            formats += ['%m-%d-%Y', '%m/%d/%Y']
        elif a > 12:
            formats += ['%d-%m-%Y', '%d/%m/%Y']
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def facts(text, field):
    if field == 'document_date':
        value = r'\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}'
    elif field in NUMERIC:
        value = NUM + r'(?![\d.,%])'
    elif field == 'payment_term':
        value = r'(?:PP|CC|PREPAID|COLLECT)\b'
    elif field == 'container_no':
        value = r'[A-Z]{4}\d{7}\b'
    else:
        value = r'[A-Z0-9][A-Z0-9_/-]*\d[A-Z0-9_/-]*'
    pattern = (r'(?<!\w)' + LABELS[field] + r'(?:\s+(?:NO\.?|NUMBER))?\s*[:：#]?\s*'
               + (r'(?:[A-Z]{3}\s+)?' if field in NUMERIC else '') + '(' + value + ')')
    found = []
    for m in re.finditer(pattern, text, re.I):
        raw = m.group(1)
        if field == 'document_date':
            parsed = date_value(raw)
        elif field in NUMERIC:
            parsed = Decimal(raw.replace(',', ''))
        elif field == 'payment_term':
            parsed = {'PREPAID': 'PP', 'COLLECT': 'CC'}.get(raw.upper(), raw.upper())
        else:
            parsed = raw.upper()
        # A multi-container list cannot be represented by one singular container_no.
        if field == 'container_no' and re.match(r'\s*[,;/]\s*[A-Z]{4}\d{7}', text[m.end():], re.I):
            continue
        if parsed is not None:
            found.append((parsed, m.group(0)))
    return found


def scope(charge, evidence):
    from .linking import linked_scope
    return linked_scope(charge,evidence)


def currency_supported(c, field, quote, scoped):
    if field not in SUMMARY:
        return True
    from .normalization import currency_mentions
    codes = set(currency_mentions(quote))
    if not codes:
        codes = {code for e in scoped for code in currency_mentions(e.text)}
    return bool(c.currency and codes == {c.currency})


def enrich_business(extraction, evidence):
    from .linking import enrich_links
    enrich_links(extraction,evidence)
    for c in extraction.charges:
        scoped = scope(c, evidence)
        for field in FIELDS:
            if c.intent == 'MODIFY' or getattr(c, field) is not None:
                continue  # Context must not become a new modification instruction.
            pool = scoped if field not in {'quantity', 'unit_price'} else [e for e in scoped if e.id in c.evidence_ids]
            found = [(v, q, e) for e in pool for v, q in facts(e.text, field)
                     if currency_supported(c, field, q, scoped)]
            # Coordinate-derived cells must include a field label and exact cell quote.
            for e in pool:
                cell = (e.structured or {}).get('business_cells', {}).get(field)
                if cell:
                    v = Decimal(cell['value']) if field in NUMERIC else cell['value']
                    found.append((v, cell['quote'], e))
            if len({v for v, _, _ in found}) == 1:
                value, quote, e = found[0]
                setattr(c, field, value)
                c.business_evidence[field] = {'evidence_id': e.id, 'quote': quote}
            elif found:
                if field!='bill_of_lading':
                    c.warnings.append(f'Conflicting business field {field}; left unresolved')
    return extraction


def validate_business(c, evidence):
    scoped = {e.id: e for e in scope(c, evidence)}
    errors = []
    for field in FIELDS:
        value = getattr(c, field)
        if value is None:
            continue
        # Existing amendment proofs own the payment-term field.
        if c.intent == 'MODIFY' and field == 'payment_term':
            continue
        proof = c.business_evidence.get(field, {})
        e = scoped.get(proof.get('evidence_id'))
        quote = proof.get('quote', '')
        accepted = False
        if e and quote and quote in e.text:
            accepted = any(v == value and q in quote for v, q in facts(quote, field))
            cell = (e.structured or {}).get('business_cells', {}).get(field, {})
            if cell.get('quote') == quote:
                try:
                    accepted |= (Decimal(cell['value']) == value if field in NUMERIC else cell['value'] == value)
                except (InvalidOperation, KeyError):
                    pass
        if field in NUMERIC and (not value.is_finite() or value < 0):
            accepted = False
        if not currency_supported(c, field, quote, list(scoped.values())):
            accepted = False
        if not accepted:
            errors.append(f'Business field {field} lacks valid scoped evidence; excluded from matching/output')
            setattr(c, field, None)
    if c.quantity is not None and c.unit_price is not None and c.amount is not None and c.amount.is_finite():
        from .credits import is_credit
        expected=abs(c.amount) if is_credit(c,evidence) else c.amount
        if abs(c.quantity * c.unit_price - expected) > Decimal('0.01'):
            errors.append('Quantity times unit price differs from charge amount')
    if all(getattr(c, f) is not None for f in ('subtotal', 'tax', 'document_total')):
        if abs(c.subtotal + c.tax - c.document_total) > Decimal('0.01'):
            errors.append('Subtotal plus tax differs from document total')
    return errors
