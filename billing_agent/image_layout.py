"""Original-column OCR table recovery; converted columns stay evidence, not extra fees."""
import re
from statistics import median
from decimal import Decimal

NUMBER=re.compile(r'-?\d+(?:,\d{3})*(?:\.\d+)?')


def grouped_boxes(boxes):
    if not boxes:
        return []
    tolerance=max(2,median(b['bottom']-b['top'] for b in boxes)*.6)
    groups=[]
    for box in sorted(boxes,key=lambda b:((b['top']+b['bottom'])/2,b['x0'])):
        center=(box['top']+box['bottom'])/2
        group=next((g for g in groups if abs((g[0]['top']+g[0]['bottom'])/2-center)<=tolerance),None)
        if group is None:groups.append([box])
        else:group.append(box)
    return [sorted(g,key=lambda b:b['x0']) for g in groups]


def frame_text(frame):
    return '\n'.join(' '.join(b['text'] for b in group) for group in grouped_boxes(frame['boxes']))


def charge_rows(frame):
    grouped=grouped_boxes(frame['boxes'])
    rows=[]
    for index,header in enumerate(grouped):
        amounts=[b for b in header if b['text'].strip().casefold()=='amount']
        currencies=[b for b in header if b['text'].strip().casefold() in {'curr','cur','currency'}]
        bases=[b for b in header if b['text'].strip().casefold()=='base']
        if not amounts or not currencies or not bases:
            continue
        # First currency and amount are the original amounts, before R.O.E./conversion.
        amount=amounts[0]
        currency=next((b for b in reversed(currencies) if b['x0']<amount['x0']),None)
        if not currency:continue
        following=next((b for b in header if b['x0']>amount['x1']),None)
        amount_left=(currency['x1']+amount['x0'])/2
        amount_right=(amount['x1']+following['x0'])/2 if following else frame['width']
        currency_left=currency['x0']-max(5,(currency['x1']-currency['x0'])/2)
        description_right=bases[0]['x0']-2
        for group in grouped[index+1:]:
            description=[b for b in group if b['x1']<=description_right]
            values=[b for b in group if amount_left<=b['x0'] and b['x1']<=amount_right and NUMBER.fullmatch(b['text'])]
            codes=[b for b in group if currency_left<=b['x0'] and b['x1']<=amount_left and re.fullmatch(r'[A-Z]{3}',b['text'])]
            if not description or len(values)!=1 or len(codes)!=1:
                continue
            name=' '.join(b['text'] for b in description)
            if re.match(r'^(TOTAL|SUBTOTAL|GRAND TOTAL)\b',name,re.I):break
            critical=description+[values[0],codes[0]]
            rows.append({'description':name,'charge_code':None,
                'amount':str(Decimal(values[0]['text'].replace(',',''))),'currency':codes[0]['text'],
                'quote':' '.join(b['text'] for b in group),
                'min_critical_score':min(b['score'] for b in critical),
                'bbox':[min(b['x0'] for b in group),min(b['top'] for b in group),max(b['x1'] for b in group),max(b['bottom'] for b in group)],
                'cells':{'description':description,'currency':codes,'amount':values},
                'column_policy':'leftmost_original_amount_before_exchange_rate'})
    return rows


def document_totals(frame,rows):
    """A unique currency/amount footer adjacent to the recognized fee table."""
    if not rows:return []
    end=max(r['bbox'][3] for r in rows)
    height=median(b['bottom']-b['top'] for b in frame['boxes'])
    candidates=[]
    for group in grouped_boxes(frame['boxes']):
        if not end<min(b['top'] for b in group)<end+height*5:
            continue
        currencies=[b for b in group if re.fullmatch(r'[A-Z]{3}',b['text'])]
        values=[b for b in group if NUMBER.fullmatch(b['text'])]
        allowed=[b for b in group if b in currencies or b in values or b['text'].upper().rstrip(':') in {'TOTAL','GRAND TOTAL'}]
        if len(group)==len(allowed) and len(currencies)==len(values)==1:
            candidates.append({'currency':currencies[0]['text'],
                'amount':str(Decimal(values[0]['text'].replace(',',''))),
                'quote':' '.join(b['text'] for b in group),
                'min_critical_score':min(b['score'] for b in group),
                'bbox':[min(b['x0'] for b in group),min(b['top'] for b in group),max(b['x1'] for b in group),max(b['bottom'] for b in group)]})
    return candidates if len(candidates)==1 else []
