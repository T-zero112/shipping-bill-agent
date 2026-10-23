"""Evidence-scoped corrections; never fill currencies from symbols or other files."""
import re
import pycountry
from .models import Extraction
from .engine import numeric_quote_supports
from .names import bind_name

SUMMARY_LABELS = {
    'TOTAL', 'SUBTOTAL', 'SUB TOTAL', 'GRAND TOTAL', 'TOTAL AMOUNT',
    'TOTAL CHARGES', 'TOTAL CHARGES TO BE PAID', 'BALANCE DUE',
    'GRAND TOTAL BALANCE DUE', 'PAID AMOUNT', 'AMOUNT PAID',
    '合计', '小计', '总计', '已付金额', '应付总额',
}


def is_summary_label(label):
    cleaned = re.sub(r'[:：\s]+', ' ', label.strip()).strip().upper()
    return cleaned in SUMMARY_LABELS


def currency_mentions(text):
    codes = {}
    for match in re.finditer(r'(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])', text):
        code = match.group(1)
        # Some ISO codes are also weight units or common English words. Without an
        # explicit currency label they are not reliable document-currency evidence.
        if code in {'KGS', 'ALL', 'TRY', 'TOP', 'MAD', 'GEL', 'SOS'}:
            labelled = re.search(r'\b(?:CURRENCY|CURR?|CCY)\s*[:：|]?\s*' + code + r'\b', text, re.I)
            if not labelled:
                continue
        if pycountry.currencies.get(alpha_3=code):
            codes.setdefault(code, match.group(0))
    return codes


def normalize_extraction(extraction, evidence):
    result = extraction.model_copy(deep=True)
    refs = {e.id: e for e in evidence}
    accepted = []
    for charge in result.charges:
        linked = [refs[i] for i in charge.evidence_ids if i in refs]
        from .linking import linked_scope,page_key
        currency_linked=list(linked)
        if any(page_key(e) for e in linked):
            for e in linked_scope(charge,evidence):
                if e.id not in charge.evidence_ids and re.search(r'\b(?:CURRENCY|CURR|CCY)\s*[:：]?\s*[A-Z]{3}\b',e.text):
                    currency_linked.append(e)
        review_sources=linked+[refs[e.parent_id] for e in linked if e.parent_id in refs]
        page_keys={(e.source,e.location.split('/')[0]) for e in linked if e.location.startswith('page:')}
        review_sources += [e for e in evidence if (e.source,e.location) in page_keys]
        for e in review_sources:
            for reason in (e.structured or {}).get('review_reasons',[]):
                if reason not in charge.warnings:
                    charge.warnings.append(reason)
        if is_summary_label(charge.charge_item):
            result.excluded_candidates.append({
                'reason': 'summary_row_not_charge', 'candidate': charge.model_dump(mode='json')})
            continue
        bind_name(charge, linked, evidence, result.normalization_events)
        if charge.currency:
            code = charge.currency.strip().upper()
            if code != charge.currency and pycountry.currencies.get(alpha_3=code):
                result.normalization_events.append({'field': 'currency', 'before': charge.currency,
                    'after': code, 'evidence_ids': charge.evidence_ids, 'rule': 'uppercase_iso_code'})
                charge.currency = code
            if linked and code not in {c for e in currency_linked for c in currency_mentions(e.text)}:
                result.normalization_events.append({'field': 'currency', 'before': charge.currency,
                    'after': None, 'rule': 'discard_currency_without_source_code',
                    'evidence_ids': charge.evidence_ids})
                charge.currency = None
        if not charge.currency:
            # Do not repair incomplete evidence links or unsupported model amounts.
            supported = bool(linked and len(linked) == len(charge.evidence_ids)
                and charge.amount_quote and numeric_quote_supports(charge.amount_quote, charge.amount)
                and any(charge.amount_quote in e.text for e in linked))
            mentions = {code for e in currency_linked for code in currency_mentions(e.text)}
            if supported and len(mentions) == 1:
                code = next(iter(mentions))
                charge.currency = code
                result.normalization_events.append({'field': 'currency', 'before': None, 'after': code,
                    'rule': 'unique_iso_currency_in_linked_evidence',
                    'evidence': [{'id': e.id, 'source': e.source, 'location': e.location, 'quote': code}
                                 for e in currency_linked if code in currency_mentions(e.text)]})
        if charge.currency and not any(charge.currency in e.text for e in linked):
            for e in currency_linked:
                match=re.search(r'\b(?:CURRENCY|CURR|CCY)\s*[:：]?\s*'+re.escape(charge.currency)+r'\b',e.text)
                if match:
                    charge.business_evidence['currency']={'evidence_id':e.id,'quote':match.group(0)}
                    break
        accepted.append(charge)
    result.charges = accepted
    totals = []
    for total in result.totals:
        # Paid amount and outstanding balance are not the gross sum of charge rows.
        quote = total.quote.strip().upper()
        if re.match(r'^(?:PAID AMOUNT|AMOUNT PAID|GRAND TOTAL BALANCE DUE|BALANCE DUE|已付|未付|应付余额)\b', quote):
            result.excluded_candidates.append({'reason': 'settlement_amount_not_gross_total',
                'candidate': total.model_dump(mode='json')})
        else:
            totals.append(total)
    result.totals = totals
    from .business import enrich_business
    return enrich_business(result, evidence)
