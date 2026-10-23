from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
from .models import Charge

ALIASES = {'handling fee': 'Handling Charge', 'h/d fee': 'Handling Charge',
    'handling charge': 'Handling Charge', 'doc fee': 'Documentation Fee',
    'thc': 'Terminal Handling Charge', 'trucking fee': 'Trucking Charge',
    'customs fee': 'Customs Clearance Fee', 'storage fee': 'Storage Charge'}
ALIASES.update({'wharfageadministrationfee':'Wharfage Administration Fee',
                'wharfage administration fee':'Wharfage Administration Fee','wharfage':'Wharfage'})


def normalize(value):
    return ALIASES.get(value.strip().lower(), value.strip())


def entity_key(value):
    return re.sub(r'\s+', ' ', value or '').strip().casefold()


def candidates(c, existing):
    """Stop at the first applicable tier, retaining ambiguity rather than guessing."""
    rows = [r for r in existing if entity_key(normalize(r['charge_details']['charge_item'])) == entity_key(normalize(c.charge_item))]
    if c.intent=='MODIFY' and c.reference_no:
        return [r for r in rows if r.get('document_info',{}).get('reference_no')==c.reference_no]
    tiers = [
        (c.reference_no, lambda r: r.get('document_info', {}).get('reference_no') == c.reference_no),
        ((c.bill_of_lading or any(f['kind'] in {'master_bl','house_bl','bl'} for f in c.transport_identifiers)) and c.currency,
         lambda r: bill_overlap(c,r.get('transport_info',{})) and r['charge_details'].get('currency') == c.currency),
        (c.booking_no and c.currency, lambda r: r.get('transport_info', {}).get('booking_no') == c.booking_no and r['charge_details'].get('currency') == c.currency),
        ((c.container_no or any(f['kind']=='container' for f in c.transport_identifiers)) and c.issuer and c.recipient and c.document_date and c.amount is not None,
         lambda r: container_set(c)==old_containers(r.get('transport_info',{}))
         and r.get('document_info', {}).get('document_date') == c.document_date
         and entity_key(r.get('transaction_party', {}).get('issuer')) == entity_key(c.issuer)
         and entity_key(r.get('transaction_party', {}).get('recipient')) == entity_key(c.recipient)
         and Decimal(str(r['charge_details'].get('amount'))) == c.amount),
    ]
    for available, predicate in tiers:
        if available:
            matches = [r for r in rows if predicate(r)]
            if matches:
                return matches
    return []


def values(info,key):
    v=info.get(key)
    return {str(x).upper() for x in (v if isinstance(v,list) else [v]) if x}


def old_containers(info):
    return values(info,'containers')|values(info,'container_no')


def container_set(c):
    return {f['value'] for f in c.transport_identifiers if f['kind']=='container'}|({c.container_no.upper()} if c.container_no else set())


def bill_overlap(c,info):
    new={kind:{f['value'] for f in c.transport_identifiers if f['kind']==kind} for kind in ['master_bl','house_bl','bl']}
    old={'master_bl':values(info,'master_bills'),'house_bl':values(info,'house_bills'),'bl':values(info,'bill_of_lading')|values(info,'bills')}
    # A generic legacy key may match either role, but must not override an explicit
    # master/house role attached to that same old value.
    old['bl']-=old['master_bl']|old['house_bl']
    if c.bill_of_lading and not any(c.bill_of_lading.upper() in new[k] for k in ['master_bl','house_bl']):
        new['bl'].add(c.bill_of_lading.upper())
    if any(new[k] and old[k] and new[k]!=old[k] for k in ['master_bl','house_bl']):
        return False
    # Multiple bills of the same role denote an aggregate scope, not an arbitrary
    # selection of the first overlapping bill.
    for k in list(new):
        if len(new[k])>1 and new[k]!=old[k]:new[k]=set()
        if len(old[k])>1 and new[k]!=old[k]:old[k]=set()
    return bool(any(new[k]&old[k] for k in new) or new['bl']&(old['master_bl']|old['house_bl'])
                or old['bl']&(new['master_bl']|new['house_bl']))


def numeric_quote_supports(quote, amount):
    if quote is None or amount is None or not amount.is_finite():
        return False
    for token in re.findall(r'(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\d.])', quote):
        try:
            if Decimal(token.replace(',', '')) == amount:
                return True
        except InvalidOperation:
            pass
    return False


def process(charge: Charge, evidence, existing, accounting_entity, entity_aliases=None):
    c = charge.model_copy(deep=True)
    issues = list(c.warnings)
    from .business import validate_business
    issues.extend(validate_business(c, evidence))
    from .linking import validate_transport,association
    issues.extend(validate_transport(c,evidence))
    issues.extend(association(c,evidence)['review_reasons'])
    from .normalization import is_summary_label
    from .financial import assess, EntityRegistry, inherit_existing
    registry = EntityRegistry(entity_aliases)
    scoped_existing = [r for r in existing if registry.same(r.get('financial_attributes',{}).get('accounting_entity'), accounting_entity)]
    from .credits import is_credit,assess_credit
    credit=assess_credit(c,evidence,existing,accounting_entity,entity_aliases) if is_credit(c,evidence) else None
    all_matches = credit['duplicates'] if credit else candidates(c,[r for r in scoped_existing if not r.get('credit_adjustment')])
    foreign_matches = candidates(c, [r for r in existing if r not in scoped_existing])
    matched = [r for r in all_matches if registry.same(r.get('financial_attributes',{}).get('accounting_entity'),accounting_entity)]
    rejected = foreign_matches
    old = matched[0] if len(matched)==1 else None
    if foreign_matches and not matched:
        issues.append('Existing bill accounting entity differs from current entity; no patch applied')
    if is_summary_label(c.charge_item):
        issues.append('Summary row is not an individual charge')
    refs = {e.id: e for e in evidence}
    valid_refs = [refs[i] for i in c.evidence_ids if i in refs]
    def remote_corroborated(e):
        meta=e.structured or {}
        origin=refs.get(meta.get('remote_response_evidence_id'))
        return bool(origin and origin.method in {'ocr_api','ocr_replay'}
            and meta.get('reconciliation') in {'exact_original_rows','merged_rows_restored_from_original'})
    if any(e.method.startswith('local_pdf_') and e.method.endswith('unverified')
           and not remote_corroborated(e) for e in valid_refs):
        issues.append('PDF processed locally; designated OCR verification still required')
    if any(e.method=='local_image_ocr_unverified' for e in valid_refs):
        issues.append('Image processed with local OCR; designated OCR verification still required')
    if not c.evidence_ids or len(valid_refs) != len(c.evidence_ids):
        issues.append('Missing or invalid evidence references')
    def supported(quote):
        return bool(quote and any(quote in e.text for e in valid_refs))
    amount_required = c.intent!='MODIFY' or 'amount' in c.explicit_changes
    if amount_required and (not supported(c.amount_quote) or not numeric_quote_supports(c.amount_quote, c.amount)):
        issues.append('Amount is missing, invalid, or not supported by exact source quote')
    if c.name_quote and not supported(c.name_quote):
        issues.append('Fee name is not supported by its primary evidence')
    currency = old['charge_details'].get('currency') if old and c.intent=='MODIFY' and 'currency' not in c.explicit_changes else c.currency
    currency_proof=c.business_evidence.get('currency',{})
    from .linking import linked_scope
    currency_supported=any(e.id==currency_proof.get('evidence_id') and currency_proof.get('quote') in e.text
        and re.fullmatch(r'(?:CURRENCY|CURR|CCY)\s*[:：]?\s*'+re.escape(c.currency or ''),currency_proof.get('quote',''))
        for e in linked_scope(c,evidence)) if currency_proof.get('quote') else False
    if c.currency and not any(c.currency in e.text for e in valid_refs) and not currency_supported:
        issues.append('Currency code lacks explicit source support')
    if old and c.intent=='MODIFY' and 'currency' not in c.explicit_changes and c.currency and c.currency!=currency:
        issues.append('Quoted currency differs from existing currency without an explicit currency change')
    if not c.actual_charge:
        issues.append('Conditional or non-incurred charge cannot be posted')
    try:
        import pycountry
        valid_currency = currency and pycountry.currencies.get(alpha_3=currency) is not None
    except ImportError:
        valid_currency = False
        issues.append('ISO currency reference dependency unavailable')
    if not valid_currency:
        issues.append('Missing or invalid ISO 4217 currency')
    if not c.reference_no and not c.bill_of_lading and not c.booking_no and not c.container_no and not c.transport_identifiers:
        issues.append('No reliable document or transport identifier')
    financial = credit['financial'] if credit else inherit_existing(assess(c,evidence,accounting_entity,entity_aliases),old,c,registry)
    posting = financial['posting_type']
    if old and c.intent=='MODIFY':
        for role in ['issuer','recipient']:
            incoming=financial['transaction_party'].get(role)
            prior=old.get('transaction_party',{}).get(role)
            if incoming and prior and not registry.same(incoming,prior):
                issues.append(f'Incoming {role} conflicts with the existing bill; party changes are not authorized')
        prior_posting=old.get('financial_attributes',{}).get('posting_type')
        if posting and prior_posting and posting!=prior_posting:
            issues.append('Incoming financial direction conflicts with the existing bill')
    issues.extend(financial['review_reasons'])
    if posting is None:
        issues.append('Accounting role or charging direction requires review')
    if not credit and c.document_type and 'credit' in c.document_type.lower():
        issues.append('Credit note requires original bill and adjustment policy review')
    out = {
        'operation': 'CREATE',
        'document_info': {'document_type': c.document_type, 'reference_no': c.reference_no, 'document_date': c.document_date},
        'transaction_party': financial['transaction_party'],
        'transport_info': {**{f: getattr(c, f) for f in ['bill_of_lading', 'booking_no', 'container_no']},
            'master_bills':sorted({f['value'] for f in c.transport_identifiers if f['kind']=='master_bl'}),
            'house_bills':sorted({f['value'] for f in c.transport_identifiers if f['kind']=='house_bl'}),
            'bills':sorted({f['value'] for f in c.transport_identifiers if f['kind']=='bl'}),
            'containers':sorted(container_set(c))},
        'document_amounts': {f: getattr(c, f) for f in ['subtotal', 'tax', 'document_total']},
        'charge_details': {'charge_item': normalize(c.charge_item), 'amount': c.amount,
                           'charge_code': c.charge_code,
                           'currency': c.currency, 'payment_term': c.payment_term,
                           'quantity': c.quantity, 'unit_price': c.unit_price},
        'financial_attributes': {'accounting_entity': accounting_entity, 'posting_type': posting},
        'audit_trail': {},
    }
    changes = {}
    if credit:
        out['document_info']['document_type']='CREDIT NOTE'
        out['charge_details']['amount']=credit['adjustment']['signed_amount']
        out['credit_adjustment']=credit['adjustment']
        out['financial_attributes']['balance_effect']='REDUCE'
    if len(matched) > 1:
        issues.append('Multiple existing charge candidates; same-name rows are not merged')
    elif matched and credit:
        out['operation']='NO_CHANGE'
        if len(matched)==1:out['id']=matched[0].get('id')
    elif matched:
        old = matched[0]
        old_entity = old.get('financial_attributes', {}).get('accounting_entity')
        if not registry.same(old_entity,accounting_entity):
            issues.append('Existing bill accounting entity differs from current entity')
        proposed = deepcopy(old)
        proposed.pop('audit_trail', None)
        if c.intent == 'MODIFY':
            from .amendments import patch_proof
            proof_errors = patch_proof(c,evidence)
            issues.extend(proof_errors)
            if not proof_errors:
                for field in c.explicit_changes:
                    value = getattr(c, field)
                    if value is None:
                        issues.append(f'Null extraction cannot clear field {field}')
                        continue
                    before = old['charge_details'].get(field)
                    equal = Decimal(str(before)) == value if field == 'amount' and before is not None else before == value
                    if not equal:
                        changes[field] = {'before': before, 'after': value}
                        proposed['charge_details'][field] = value
            proposed['operation'] = 'UPDATE' if changes else 'NO_CHANGE'
        else:
            differing = []
            for field in ['amount', 'currency', 'payment_term', 'quantity', 'unit_price']:
                value = getattr(c, field)
                old_value = old['charge_details'].get(field)
                equal = Decimal(str(old_value)) == value if field in {'amount','quantity','unit_price'} and old_value is not None and value is not None else old_value == value
                if value is not None and not equal:
                    differing.append(field)
            if differing:
                issues.append('Observed differences without explicit modification: ' + ', '.join(differing))
            proposed['operation'] = 'NO_CHANGE'
        out = proposed
    elif c.intent == 'MODIFY':
        issues.append('Modification target not found; cannot create instead')
    out['audit_trail'] = {
        'ai_reasoning': ['Matched existing bill' if matched else 'No existing match',
                         'Only explicitly requested fields may change'],
        'evidence': [e.model_dump() for e in valid_refs],
        'raw_charge_item': c.source_description or c.charge_item,
        'name_quote': c.name_quote,
        'financial_assessment': financial,
        'party_evidence': [refs[i].model_dump() for i in c.party_evidence_ids if i in refs],
        'conflict_evidence': [refs[i].model_dump() for i in c.conflict_evidence_ids if i in refs],
        'field_evidence': c.field_evidence,
        'business_evidence': c.business_evidence,
        'document_association':association(c,evidence),
        'transport_identifiers':c.transport_identifiers,
        'business_source_evidence': [refs[i].model_dump() for i in sorted({p.get('evidence_id') for p in c.business_evidence.values()} - {None}) if i in refs],
        'change_quote': c.change_quote,
        'proposed_changes': changes,
        'review_reasons': issues,
        'matched_bill_ids': [r.get('id') for r in matched],
        'rejected_entity_scope_bill_ids': [r.get('id') for r in rejected],
    }
    remote_ids={(e.structured or {}).get('remote_response_evidence_id') for e in valid_refs}-{None}
    if remote_ids:
        out['audit_trail']['remote_ocr_source_evidence']=[refs[i].model_dump() for i in sorted(remote_ids) if i in refs]
    if credit:
        out['audit_trail']['ai_reasoning']=['Explicit original invoice association checked',
            'Separate negative credit; preserve original AR/AP and original invoice amount']
    if issues:
        out['operation'] = 'PENDING_REVIEW'
    return out


def process_batch(charges, evidence, existing, entity, totals=None, entity_aliases=None):
    results = []
    fingerprints = set()
    processed_charges = []
    previous_requests = {}
    for c in charges:
        from .business import validate_business
        c = c.model_copy(deep=True)
        c.warnings.extend(validate_business(c, evidence))
        # Same-page evidence may contain two genuinely identical rows: flag, never drop.
        from .requests import request_key
        key = request_key(c,evidence,entity) if c.intent=='MODIFY' else (tuple(sorted(c.evidence_ids)), c.amount_quote, c.charge_item, str(c.amount), c.currency)
        duplicate = key in fingerprints
        fingerprints.add(key)
        processed_charges.append(c)
        result = process(c, evidence, existing, entity, entity_aliases)
        if duplicate and c.intent=='MODIFY':
            prior=previous_requests[key]
            result=deepcopy(prior)
            result['operation']='NO_CHANGE' if prior['operation'] in {'UPDATE','NO_CHANGE'} else 'PENDING_REVIEW'
            result['audit_trail']['proposed_changes']={}
            result['audit_trail']['duplicate_instruction']=True
        elif duplicate:
            result['operation'] = 'PENDING_REVIEW'
            result['audit_trail']['review_reasons'].append('Repeated extraction claim; verify distinct source rows')
            for prior_c, prior_r in zip(processed_charges[:-1], results):
                prior_key = (tuple(sorted(prior_c.evidence_ids)), prior_c.amount_quote, prior_c.charge_item, str(prior_c.amount), prior_c.currency)
                if prior_key == key:
                    prior_r['operation'] = 'PENDING_REVIEW'
                    prior_r['audit_trail']['review_reasons'].append('Repeated extraction claim; verify distinct source rows')
        results.append(result)
        previous_requests.setdefault(key,result)
    for total in totals or []:
        source = next((e for e in evidence if e.id == total.evidence_id), None)
        source_ids = {e.id for e in evidence if source and e.source == source.source}
        linked = [(c, r) for c, r in zip(processed_charges, results)
                  if c.reference_no == total.reference_no and c.currency == total.currency and c.actual_charge
                  and source_ids.intersection(c.evidence_ids)]
        supported = source and total.quote in source.text and numeric_quote_supports(total.quote, total.amount)
        from .credits import is_credit
        total_amount = sum((abs(c.amount) if is_credit(c,evidence) and total.amount>=0 else c.amount
                           for c, _ in linked if c.amount is not None and c.amount.is_finite()), Decimal(0))
        taxes = {c.tax for c, _ in linked if c.tax is not None}
        tax = next(iter(taxes)) if len(taxes)==1 and all(c.tax is not None for c,_ in linked) else Decimal(0)
        if not supported or any(c.amount is None for c,_ in linked) or abs(total_amount+tax-total.amount) > Decimal('0.01'):
            for _, result in linked:
                result['operation'] = 'PENDING_REVIEW'
                result['audit_trail']['review_reasons'].append('Document total unsupported or line sum mismatch')
    # Subtotal/gross checks are scoped by source, reference and currency, never by
    # reference alone (different files can carry the same invoice number).
    groups = {}
    refs = {e.id:e for e in evidence}
    for c, r in zip(processed_charges, results):
        sources = {refs[i].source for i in c.evidence_ids if i in refs}
        if c.intent != 'MODIFY' and c.actual_charge and len(sources)==1 and c.reference_no and c.currency:
            groups.setdefault((next(iter(sources)),c.reference_no,c.currency),[]).append((c,r))
    for rows in groups.values():
        from .credits import is_credit
        amounts = [abs(c.amount) if c.amount is not None and is_credit(c,evidence) else c.amount for c,_ in rows]
        for field in ['subtotal','document_total']:
            values = {getattr(c,field) for c,_ in rows if getattr(c,field) is not None}
            if not values:
                continue
            taxes = {c.tax for c,_ in rows if c.tax is not None}
            adjustment = next(iter(taxes)) if field=='document_total' and len(taxes)==1 else Decimal(0)
            mismatch = (len(values)>1 or len(taxes)>1 or any(v is None for v in amounts)
                        or abs(sum((v for v in amounts if v is not None),Decimal(0))+adjustment-next(iter(values)))>Decimal('0.01'))
            if mismatch:
                for _, r in rows:
                    r['operation']='PENDING_REVIEW'
                    r['audit_trail']['review_reasons'].append(f'Document {field} differs from scoped line sum or conflicts')
    credit_groups={}
    for r in results:
        if r.get('credit_adjustment') and r['operation']=='CREATE':
            credit_groups.setdefault(r['credit_adjustment']['original_bill_id'],[]).append(r)
    for group in credit_groups.values():
        total_credit=sum((r['credit_adjustment']['credit_amount'] for r in group),Decimal(0))
        keys=[(r['document_info']['reference_no'],r['charge_details']['charge_item']) for r in group]
        if len(keys)!=len(set(keys)) or total_credit>min(r['credit_adjustment']['remaining_before'] for r in group):
            for r in group:
                r['operation']='PENDING_REVIEW'
                r['financial_attributes']['posting_type']=None
                reason='Batch credit aggregate exceeds remaining amount or repeats same credit line identity'
                r['credit_adjustment']['posting_type']=None
                r['audit_trail']['financial_assessment'].update(posting_type=None,status='PENDING_REVIEW')
                r['audit_trail']['financial_assessment']['review_reasons'].append(reason)
                r['audit_trail']['review_reasons'].append(reason)
    # Different claims must not independently update the same old bill in one batch.
    target_counts = {}
    for r in results:
        if r['audit_trail'].get('duplicate_instruction'):
            continue
        for identity in r['audit_trail']['matched_bill_ids']:
            if identity:
                target_counts[identity] = target_counts.get(identity, 0) + 1
    for r in results:
        if any(target_counts.get(i,0) > 1 for i in r['audit_trail']['matched_bill_ids'] if i):
            r['operation'] = 'PENDING_REVIEW'
            r['audit_trail']['review_reasons'].append('Multiple input claims target the same existing bill')
    return results
