"""Conservative adapter for the observed designated OCR response, with source proofs."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import re

from .models import Charge, Extraction


def money(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).replace(',', ''))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def key(value):
    return re.sub(r'\s+', ' ', value.strip()).casefold()


def adapt(parser, data, name, result, raw):
    """Raw page_index semantics are unknown; only a single-page/single-item is bound."""
    payload = result.get('data')
    items = payload.get('list') if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        parser.issues.append({'source':name,'status':'unresolved_ocr',
            'warning':'Unknown remote OCR list schema; retained raw response'})
        return
    native = []
    if data.startswith(b'%PDF-'):
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        if len(reader.pages) == 1 and len(items) == 1:
            # Reuse proven local column/role parsers. Never upload more files here.
            from .parsers import Parser
            local = Parser(pdf_mode='local', image_mode='local')
            local.parse(data, name)
            native = local.evidence
            for e in native:
                e.structured = {**(e.structured or {}), 'remote_context_only': True}
                e.source_digest = raw.source_digest
                if e.id not in {x.id for x in parser.evidence}:
                    parser.evidence.append(e)
    native_rows = [e for e in native if (e.structured or {}).get('kind') == 'pdf_charge_row']
    from .linking import page_groups
    _, identities, _, rejected = page_groups(native)
    references = set(identities.values()) if not rejected else set()
    local_reference = next(iter(references)) if len(references) == 1 else None

    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get('chargeList'), list):
            parser.issues.append({'source': name, 'status': 'unresolved_ocr',
                'warning': 'Unknown remote OCR bill/chargeList schema; retained raw response'})
            continue
        remote_rows = item['chargeList']
        remote_groups = defaultdict(list)
        for row in remote_rows:
            if isinstance(row, dict) and isinstance(row.get('chargeItem'), str):
                remote_groups[key(row['chargeItem'])].append(row)
            else:
                parser.issues.append({'source': name, 'status': 'unresolved_ocr',
                    'warning': 'Malformed remote OCR charge row; retained raw response'})
        local_groups = defaultdict(list)
        for e in native_rows:
            local_groups[key(e.structured['description'])].append(e)
        for description, rows in remote_groups.items():
            originals = local_groups.get(description, [])
            remote_amounts = [money(row.get('amount')) for row in rows]
            native_amounts = [money(e.structured['amount']) for e in originals]
            exact = bool(originals and None not in remote_amounts and Counter(remote_amounts) == Counter(native_amounts))
            merged = bool(len(rows) == 1 and len(originals) > 1 and remote_amounts[0] is not None
                          and sum(native_amounts, Decimal(0)) == remote_amounts[0])
            # Never split a container-specific/foreign-currency aggregation without proof.
            constrained = any(row.get('chargeCtr') or row.get('currency') for row in rows)
            if merged and constrained:
                merged = False
            if exact or merged:
                remote_ref = item.get('invoiceNumber') or None
                remote_currency = {str(row['currency']).upper() for row in rows if row.get('currency')}
                for e in originals:
                    cells = e.structured
                    reasons = []
                    if remote_ref and local_reference and remote_ref.upper() != local_reference:
                        reasons.append('Remote OCR invoice number conflicts with original PDF')
                    if remote_currency and remote_currency != {cells.get('currency')}:
                        reasons.append('Remote OCR currency conflicts with original PDF')
                    if any(row.get('chargeCtr') for row in rows):
                        reasons.append('Remote OCR fee-container attribution requires original row proof')
                    if not local_reference:
                        reasons.append('Document number has no unique explicit source proof')
                    charge = Charge(reference_no=local_reference, charge_item=cells['description'],
                        charge_code=cells.get('charge_code'), source_description=cells['description'],
                        name_quote=cells['description'], amount=cells['amount'], currency=cells.get('currency'),
                        amount_quote=cells['quote'], evidence_ids=[e.id], warnings=reasons)
                    e.structured.update(remote_charge=charge.model_dump(mode='json'),
                        remote_response_evidence_id=raw.id, remote_item_index=index,
                        remote_rows=rows, remote_page_index_raw=item.get('page_index'),
                        reconciliation='merged_rows_restored_from_original' if merged else 'exact_original_rows',
                        page_binding='single PDF page and single remote item; not inferred from page_index')
                    parser.remote_adapter_events.append({'source': name, 'rule': e.structured['reconciliation'],
                        'evidence_id': e.id, 'remote_response_evidence_id': raw.id,
                        'remote_amounts': [str(x) for x in remote_amounts], 'source_amount': cells['amount'],
                        'reference_recovered': not remote_ref and bool(local_reference),
                        'currency_recovered': not remote_currency and bool(cells.get('currency'))})
            else:
                for row_index, row in enumerate(rows):
                    text = json.dumps(row, ensure_ascii=False, sort_keys=True)
                    reasons = ['Remote OCR row has no unique original physical-row verification; review required']
                    if originals:
                        reasons.append('Remote OCR amount/count conflicts with original fee rows; no automatic split')
                    amount = money(row.get('amount'))
                    if amount is None:
                        reasons.append('Remote OCR amount is invalid or missing')
                    e = parser.add(name, f'ocr:item:{index+1}/fee:{description}:{row_index+1}', text,
                        'ocr_adapted', source_digest=raw.source_digest,
                        structured={'kind': 'remote_ocr_charge', 'remote_context_only': True,
                            'remote_response_evidence_id': raw.id, 'remote_page_index_raw': item.get('page_index'),
                            'review_reasons': reasons})
                    charge = Charge(charge_item=row['chargeItem'], amount=amount, amount_quote=text,
                        name_quote=row['chargeItem'], evidence_ids=[e.id], warnings=reasons)
                    # Missing fields stay null; raw data remains available without treating
                    # unknown parties/transport strings as validated document facts.
                    e.structured['remote_charge'] = charge.model_dump(mode='json')
                    parser.remote_adapter_events.append({'source': name, 'rule': 'unverified_remote_row',
                        'evidence_id': e.id, 'remote_response_evidence_id': raw.id})
        unmatched = set(local_groups) - set(remote_groups)
        if unmatched:
            raw.structured.setdefault('review_reasons', []).append('Remote OCR omitted original fee descriptions')
            parser.issues.append({'source': name, 'warning': 'Remote OCR omitted original fee descriptions',
                'descriptions': sorted(unmatched)})
            for e in native_rows:
                if (e.structured or {}).get('remote_charge'):
                    e.structured['remote_charge']['warnings'].append('Remote OCR omitted original fee descriptions')


def charges(evidence):
    return Extraction(charges=[Charge.model_validate(e.structured['remote_charge']) for e in evidence
        if (e.structured or {}).get('remote_charge')])
