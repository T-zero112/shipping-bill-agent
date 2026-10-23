"""Bind descriptions to the same physical row/statement as their amounts."""
import re
from decimal import Decimal
from .engine import numeric_quote_supports

FEE_PHRASE = re.compile(
    r'(?<![A-Za-z0-9])(?:H/C|H/D(?:\s+Fee)?|Handling\s+(?:Fee|Charge)|'
    r'Doc(?:umentation)?\s+Fee|THC|Trucking\s+(?:Fee|Charge)|Storage\s+(?:Fee|Charge)|'
    r'Customs(?:\s+Clearance)?\s+Fee)(?![A-Za-z])', re.I)


def bind_name(charge, linked, evidence, events):
    if len(linked) != 1:
        return
    source = linked[0]
    cells = source.structured or {}
    description = None
    rule = None
    if cells.get('kind') in {'pdf_charge_row','image_charge_row'}:
        if charge.amount is not None and charge.amount == Decimal(cells['amount']):
            description = cells['description']
            charge.charge_code = cells.get('charge_code')
            rule = 'pdf_description_column_with_same_row_amount'
    elif cells.get('kind') == 'email_statement':
        matches = list(FEE_PHRASE.finditer(source.text))
        if len(matches)==1 and numeric_quote_supports(source.text, charge.amount):
            description = matches[0].group(0)
            rule = 'fee_phrase_in_same_monetary_statement'
            # The monetary statement is primary evidence. Other statements are conflicts,
            # never alternative names or amount evidence.
            charge.amount_quote = source.text
            for other in evidence:
                if other.id != source.id and other.source == source.source and other.parent_id == source.parent_id:
                    if description.casefold() in other.text.casefold() and re.search(r'\bfree\b', other.text, re.I):
                        if other.id not in charge.conflict_evidence_ids:
                            charge.conflict_evidence_ids.append(other.id)
                        warning = 'Free-service statement conflicts with monetary fee candidate; confirm service scope and applicability'
                        if warning not in charge.warnings:
                            charge.warnings.append(warning)
    if description and description in source.text:
        if description != charge.charge_item:
            events.append({'field':'charge_item','before':charge.charge_item,'after':description,
                           'rule':rule,'evidence_ids':[source.id],'quote':description})
        charge.charge_item = description
        charge.source_description = description
        charge.name_quote = description
