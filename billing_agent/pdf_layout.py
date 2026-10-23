"""Conservative header-driven coordinate extraction for two common charge-table layouts.

No filenames, fee names, amounts or document-specific coordinates are used as rules.
Unsupported tables retain the existing text/model path.
"""
import re
from decimal import Decimal

NUMBER = re.compile(r'-?\d+(?:,\d{3})*(?:\.\d+)?')


def lines(words, tolerance=3):
    groups = []
    for word in sorted(words, key=lambda w: (w['top'], w['x0'])):
        group = next((g for g in groups if abs(g[0]['top']-word['top']) <= tolerance), None)
        if group is None:
            groups.append([word])
        else:
            group.append(word)
    return [sorted(g, key=lambda w: w['x0']) for g in groups]


def charge_rows(page):
    grouped = lines(page.extract_words(x_tolerance=2, y_tolerance=3))
    result = []
    for index, header in enumerate(grouped):
        labels = {w['text'].upper(): w for w in header}
        simple = {'CHARGES', 'CUR', 'AMOUNT'}.issubset(labels)
        debit = {'DESCRIPTION', 'UNIT', 'DEBIT(+)', 'CREDIT(-)'}.issubset(labels)
        if not simple and not debit:
            continue
        if simple:
            left = labels['CHARGES']['x0']-5
            right = labels['CUR']['x0']-1
            amount_left = labels['AMOUNT']['x0']-5
            amount_right = labels.get('TYPE', {'x0': page.width})['x0']
        else:
            left = labels['DESCRIPTION']['x0']-5
            right = labels['UNIT']['x0']-3
            previous = labels.get('P/C', labels['QTY'])
            amount_left = (previous['x1']+labels['DEBIT(+)']['x0'])/2
            amount_right = (labels['DEBIT(+)']['x1']+labels['CREDIT(-)']['x0'])/2
        currency = None
        currency_header = None
        for group in grouped[:index]:
            for i, word in enumerate(group[:-1]):
                if word['text'].upper().rstrip(':') == 'CURRENCY' and re.fullmatch(r'[A-Z]{3}', group[i+1]['text']):
                    currency = group[i+1]['text']
                    currency_header = {'text': 'CURRENCY '+currency,
                        'bbox': [word['x0'], word['top'], group[i+1]['x1'], group[i+1]['bottom']]}
        for row in grouped[index+1:]:
            text = ' '.join(w['text'] for w in row)
            if re.match(r'^(TOTAL|SUBTOTAL|GRAND TOTAL|PAID AMOUNT)\b', text, re.I):
                break
            description_words = [w for w in row if left <= w['x0'] and w['x1'] <= right]
            amounts = [w for w in row if amount_left <= w['x0'] and w['x1'] <= amount_right
                       and NUMBER.fullmatch(w['text'])]
            if not description_words or len(amounts) != 1:
                continue
            code = None
            if (simple and len(description_words)>1
                and description_words[1]['x0']-description_words[0]['x1'] > 8
                and re.fullmatch(r'[A-Z][A-Z0-9_-]*\d[A-Z0-9_-]*', description_words[0]['text'])):
                code = description_words[0]['text']
                description_words = description_words[1:]
            if not description_words:
                continue
            description = ' '.join(w['text'] for w in description_words)
            row_currency = currency
            if simple:
                found = [w['text'] for w in row if labels['CUR']['x0']-2 <= w['x0'] < amount_left
                         and re.fullmatch(r'[A-Z]{3}', w['text'])]
                row_currency = found[0] if len(found)==1 else None
            business_cells = {}
            if debit:
                columns = sorted(header, key=lambda w: w['x0'])
                for label, field in [('RATE', 'unit_price'), ('QTY', 'quantity'), ('P/C', 'payment_term')]:
                    if label not in labels:
                        continue
                    h = labels[label]
                    pos = columns.index(h)
                    lo = (columns[pos-1]['x1']+h['x0'])/2 if pos else h['x0']-5
                    hi = (h['x1']+columns[pos+1]['x0'])/2 if pos+1<len(columns) else page.width
                    values = [w['text'] for w in row if lo <= (w['x0']+w['x1'])/2 < hi]
                    if len(values) == 1:
                        raw = values[0]
                        if field == 'payment_term' and raw in {'P','C','PP','CC'}:
                            business_cells[field] = {'value': {'P':'PP','C':'CC'}.get(raw,raw), 'quote':raw}
                        elif field != 'payment_term' and NUMBER.fullmatch(raw):
                            business_cells[field] = {'value':raw.replace(',',''), 'quote':raw}
            result.append({'description': description, 'charge_code': code,
                'business_cells': business_cells,
                'amount': str(Decimal(amounts[0]['text'].replace(',', ''))), 'currency': row_currency,
                'currency_header': currency_header,
                'quote': text,
                'bbox': [min(w['x0'] for w in row), min(w['top'] for w in row),
                         max(w['x1'] for w in row), max(w['bottom'] for w in row)],
                'description_bbox': [description_words[0]['x0'], min(w['top'] for w in description_words),
                                     description_words[-1]['x1'], max(w['bottom'] for w in description_words)]})
    return result
