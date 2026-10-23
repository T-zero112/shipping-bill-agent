"""Extract separately labelled party mentions; a letterhead is not an invoice issuer."""
import re
from .pdf_layout import lines

LABELS = {'ISSUER':'issuer','ISSUED BY':'issuer','BILL TO':'recipient','BILLED TO':'recipient',
          'BENEFICIARY':'beneficiary','SHIPPER':'shipper','CONSIGNEE':'consignee',
          'NOTIFY':'notify','PAYER':'payer','PAYEE':'payee'}
LABELS.update({'开票方':'issuer','受票方':'recipient','付款方':'payer','收款方':'payee'})
COMPANY = re.compile(r'(?:有限公司|\bLIMITED\b|\bLTD\.?\b|\bLLC\b|\bINC\.?\b|\bBV\b)',re.I)


def party_blocks(page):
    grouped = lines(page.extract_words(x_tolerance=2,y_tolerance=3))
    result = []
    def emit(role,entity,words):
        if entity and words:
            result.append({'role':role,'entity':entity,'quote':' '.join(w['text'] for w in words),
                'bbox':[min(w['x0'] for w in words),min(w['top'] for w in words),
                        max(w['x1'] for w in words),max(w['bottom'] for w in words)]})
    for index, row in enumerate(grouped):
        text = ' '.join(w['text'] for w in row)
        company = COMPANY.search(text)
        trailing = text[company.end():].strip(' .') if company else ''
        if row[0]['top'] < page.height*.13 and company and not trailing and not any(text.upper().startswith(k) for k in LABELS):
            emit('header_party',text,row)
        payee = re.match(r'^GRAND TOTAL BALANCE DUE TO\s+(.+?)\s+[A-Z]{3}\s+[-\d,.]+$',text)
        if payee:
            emit('payee',payee.group(1),row)
        for side in (0,1):
            lo,hi = (0,page.width/2) if side==0 else (page.width/2,page.width)
            segment = [w for w in row if lo <= w['x0'] < hi]
            value = ' '.join(w['text'] for w in segment)
            for label,role in LABELS.items():
                match = re.match(r'^'+re.escape(label)+r'(?=\s|:|$)\s*(?::\s*)?(.*)$',value)
                if not match:
                    continue
                entity = match.group(1).strip()
                words = segment
                if not entity and index+1<len(grouped):
                    following = [w for w in grouped[index+1] if lo <= w['x0'] < hi]
                    next_text = ' '.join(w['text'] for w in following)
                    if COMPANY.search(next_text):
                        entity = next_text
                        words = segment+following
                elif entity and not COMPANY.search(entity) and index+1<len(grouped):
                    following = [w for w in grouped[index+1] if lo <= w['x0'] < hi]
                    next_text = ' '.join(w['text'] for w in following)
                    if COMPANY.search(next_text) and not any(next_text.startswith(k) for k in LABELS):
                        entity += ' '+next_text
                        words = segment+following
                company = COMPANY.search(entity)
                if company:
                    end = company.end()
                    if entity[end:end+1]=='.':
                        end += 1
                    entity = entity[:end]
                elif role in {'shipper','consignee','notify'}:
                    entity = ''  # Narrative text and label continuations are not company identities.
                if entity:
                    emit(role,entity,words)
                break
    return result
