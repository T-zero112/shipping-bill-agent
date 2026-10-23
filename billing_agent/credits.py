"""Credit notes create separate negative adjustments; never overwrite the invoice."""
from decimal import Decimal, InvalidOperation
import re
from .linking import linked_scope


def is_credit(c,evidence):
    return any(re.search(r'^\s*(?:CREDIT\s+NOTE|贷项通知|红字)',e.text,re.I|re.M)
               for e in credit_scope(c,evidence))


def credit_scope(c,evidence):
    scoped=linked_scope(c,evidence)
    statement_parents={e.parent_id for e in scoped if (e.structured or {}).get('kind')=='email_statement'}
    return [e for e in scoped if e.id not in statement_parents and not (e.structured or {}).get('quoted')]


def enrich_credit_identity(c,evidence):
    if c.intent=='MODIFY':return
    own=[]
    for e in credit_scope(c,evidence):
        for m in re.finditer(r'^\s*CREDIT\s+NOTE\s*(?:NO\.?|NUMBER|#)\s*[:：]?\s*([A-Z0-9][A-Z0-9_/-]*\d[A-Z0-9_/-]*)\s*$',e.text,re.I|re.M):
            own.append((m.group(1).upper(),e,m.group(0)))
    if len({v for v,_,_ in own})==1:
        value,e,quote=own[0]
        c.reference_no=value;c.document_type='CREDIT NOTE'
        c.business_evidence['credit_reference_no']={'evidence_id':e.id,'quote':quote}


def assess_credit(c,evidence,existing,entity,aliases=None):
    from .financial import assess,EntityRegistry
    from .engine import normalize,entity_key
    registry=EntityRegistry(aliases)
    scoped=credit_scope(c,evidence)
    reasons=[];proofs=[]
    if any(re.search(r'\b(?:DRAFT|PRO\s*FORMA|SUBJECT TO APPROVAL|IF APPROVED|CANCELLED|VOID)\b|待审批|作废',e.text,re.I) for e in scoped):
        reasons.append('Draft, conditional or cancelled credit requires review')
    if any(re.search(r'\b(?:CASH\s+REFUND|REFUND\s+PAID)\b|现金退款|已退款',e.text,re.I) for e in scoped):
        reasons.append('Cash refund requires settlement-specific review')
    refs=set();own=set()
    for e in scoped:
        for line in e.text.splitlines():
            m=re.match(r'\s*(?:ORIGINAL\s+(?:INVOICE|DEBIT NOTE)|原发票|原单)\s*(?:NO\.?|NUMBER|#)?\s*[:：]?\s*([A-Z0-9][A-Z0-9_/-]*\d[A-Z0-9_/-]*)\s*$',line,re.I)
            if m:
                refs.add(m.group(1).upper());proofs.append({'field':'original_reference_no','evidence_id':e.id,'quote':line})
            m=re.match(r'\s*CREDIT\s+NOTE\s*(?:NO\.?|NUMBER|#)\s*[:：]?\s*([A-Z0-9][A-Z0-9_/-]*\d[A-Z0-9_/-]*)\s*$',line,re.I)
            if m:own.add(m.group(1).upper());proofs.append({'field':'credit_reference_no','evidence_id':e.id,'quote':line})
    if len(refs)!=1:reasons.append('Credit note requires one explicit original invoice reference')
    if len(own)!=1 or c.reference_no not in own:reasons.append('Credit note own reference missing, ambiguous or inconsistent')
    original_ref=next(iter(refs)) if len(refs)==1 else None
    matches=[r for r in existing if original_ref and r.get('document_info',{}).get('reference_no')==original_ref
             and registry.same(r.get('financial_attributes',{}).get('accounting_entity'),entity)
             and entity_key(normalize(r.get('charge_details',{}).get('charge_item','')))==entity_key(normalize(c.charge_item))
             and not r.get('credit_adjustment') and 'credit' not in (r.get('document_info',{}).get('document_type') or '').lower()]
    old=matches[0] if len(matches)==1 else None
    if len(matches)!=1:reasons.append('Original credit target missing or ambiguous in current accounting entity')
    financial=assess(c,evidence,entity,aliases)
    permitted={'Credit note requires original bill and adjustment policy review','No complete evidence-backed creditor/debtor relationship'}
    reasons.extend(r for r in financial['review_reasons'] if r not in permitted)
    amount=abs(c.amount) if c.amount is not None and c.amount.is_finite() else None
    if amount is None or amount<=0:reasons.append('Credit amount must be a nonzero finite source amount')
    posting=None;remaining=None;original_amount=None;used=None
    prior=[]
    duplicate=[]
    if old:
        if not old.get('id'):reasons.append('Original invoice requires stable bill ID')
        for role in ['issuer','recipient']:
            if not registry.same(financial['transaction_party'].get(role),old.get('transaction_party',{}).get(role)):
                reasons.append(f'Credit {role} missing or conflicts with original invoice')
        if c.currency!=old.get('charge_details',{}).get('currency'):reasons.append('Credit and original currencies differ')
        roles=old.get('transaction_party',{})
        posting='AR' if registry.same(entity,roles.get('issuer')) else 'AP' if registry.same(entity,roles.get('recipient')) else None
        if not posting or old.get('financial_attributes',{}).get('posting_type')!=posting:
            reasons.append('Original invoice accounting direction inconsistent with current entity')
        prior=[r for r in existing if registry.same(r.get('financial_attributes',{}).get('accounting_entity'),entity)
               and r.get('credit_adjustment',{}).get('original_bill_id')==old.get('id')]
        duplicate=[r for r in existing if registry.same(r.get('financial_attributes',{}).get('accounting_entity'),entity)
                   and r.get('document_info',{}).get('reference_no')==c.reference_no
                   and entity_key(normalize(r.get('charge_details',{}).get('charge_item','')))==entity_key(normalize(c.charge_item))]
        try:
            original_amount=Decimal(str(old['charge_details']['amount']))
            if not original_amount.is_finite() or original_amount<=0:raise ValueError()
            payment=old.get('settlement',{})
            if Decimal(str(payment.get('paid_amount',0)))!=0 or ('outstanding_amount' in payment and Decimal(str(payment['outstanding_amount']))!=original_amount):
                reasons.append('Original settlement differs from face amount; refund/settlement policy requires review')
            used=Decimal(0)
            for r in prior:
                signed=Decimal(str(r['charge_details']['amount']))
                if not signed.is_finite() or signed>=0 or r['charge_details'].get('currency')!=c.currency:raise ValueError()
                if r not in duplicate:used+=abs(signed)
            remaining=original_amount-used
            if amount is not None and amount>remaining:reasons.append('Credit exceeds remaining original amount')
        except (InvalidOperation,ValueError,KeyError):
            original_amount=None;used=None;remaining=None
            reasons.append('Original or previous credit amount invalid; remaining credit cannot be verified')
        if duplicate:
            try:duplicate_amount=Decimal(str(duplicate[0]['charge_details'].get('amount',0)))
            except (InvalidOperation,KeyError):duplicate_amount=None
            if (len(duplicate)!=1 or not duplicate[0].get('credit_adjustment') or duplicate[0]['credit_adjustment'].get('original_bill_id')!=old.get('id')
                    or duplicate[0]['charge_details'].get('currency')!=c.currency or duplicate_amount!=-(amount or 0)
                    or any(not registry.same(duplicate[0].get('transaction_party',{}).get(role),old.get('transaction_party',{}).get(role)) for role in ['issuer','recipient'])
                    or duplicate[0].get('financial_attributes',{}).get('posting_type')!=posting):
                reasons.append('Repeated credit reference conflicts with previous adjustment')
    if c.intent=='MODIFY':reasons.append('Credit note cannot be applied as an ordinary invoice field patch')
    if not old or reasons:posting=None
    financial.update(posting_type=posting,status='RESOLVED_CREDIT' if posting else 'PENDING_REVIEW',review_reasons=list(dict.fromkeys(reasons)))
    adjustment={'original_reference_no':original_ref,'original_bill_id':old.get('id') if old else None,
                'original_amount':original_amount,'credited_before':used,
                'previous_credit_bill_ids':[r.get('id') for r in prior if r not in duplicate],
                'credit_amount':amount,'signed_amount':-amount if amount is not None else None,
                'remaining_before':remaining,'remaining_after':remaining-amount if remaining is not None and amount is not None else None,
                'balance_effect':'REDUCE','posting_type':posting,'proofs':proofs,
                'policy':'Preserve original AR/AP; create separate negative credit, do not reverse counterparties or edit original amount'}
    return {'financial':financial,'adjustment':adjustment,'duplicates':duplicate,'original':old}
