"""Evidence-scoped roles and deterministic accounting perspective; no title-only AR/AP."""
import re


def key(value):
    return re.sub(r'\s+', ' ', value or '').strip().casefold()


class EntityRegistry:
    def __init__(self, aliases=None):
        self.lookup = {}
        if aliases is not None and not isinstance(aliases,dict):
            raise ValueError('Entity aliases must be a JSON object')
        for canonical, names in (aliases or {}).items():
            if not isinstance(names, list) or not all(isinstance(n,str) and n.strip() for n in [canonical]+names):
                raise ValueError('Entity aliases must map a canonical name to a list of nonempty names')
            for name in [canonical]+names:
                if key(name) in self.lookup and self.lookup[key(name)] != canonical:
                    raise ValueError(f'Ambiguous entity alias: {name}')
                self.lookup[key(name)] = canonical

    def canonical(self, name):
        return self.lookup.get(key(name), name.strip() if name else None)

    def same(self, first, second):
        return bool(first and second and key(self.canonical(first)) == key(self.canonical(second)))


def assess(charge, evidence, accounting_entity, aliases=None):
    registry = EntityRegistry(aliases)
    refs = {e.id:e for e in evidence}
    primary = [refs[i] for i in charge.evidence_ids if i in refs]
    sources = {e.source for e in primary}
    pages = {(e.source,e.location.split('/')[0]) for e in primary if e.location.startswith('page:')}
    from .linking import linked_scope,page_key
    if pages:
        pages|={page_key(e) for e in linked_scope(charge,evidence) if page_key(e)}
    facts = []
    reasons = []
    if len(sources)>1:
        reasons.append('Multiple document contexts cannot jointly establish financial parties')
    statement_parents={e.parent_id for e in evidence if (e.structured or {}).get('kind')=='email_statement'}
    scope_evidence = [e for e in evidence if e.source in sources and e.id not in statement_parents
        and not (e.structured or {}).get('quoted')
        and (not pages or (e.source,e.location.split('/')[0]) in pages)]

    def add(role, entity, e, quote):
        if entity and entity in quote and quote in e.text:
            item = {'role':role,'entity':entity,'canonical_entity':registry.canonical(entity),
                    'evidence_id':e.id,'source':e.source,'location':e.location,'quote':quote}
            if item not in facts:
                facts.append(item)

    for e in scope_evidence:
        structured = e.structured or {}
        if structured.get('kind')=='party_role':
            add(structured['role'],structured['entity'],e,e.text)
        elif not e.method.startswith('local_pdf_'):
            # Non-PDF parsers may preserve explicit, single-line labels directly.
            for line in e.text.splitlines():
                for label,role in [('ISSUED BY','issuer'),('ISSUER','issuer'),('BILL TO','recipient'),
                                   ('BILLED TO','recipient'),('BENEFICIARY','beneficiary'),
                                   ('PAYER','payer'),('PAYEE','payee'),('开票方','issuer'),('受票方','recipient'),
                                   ('付款方','payer'),('收款方','payee')]:
                    match = re.match(r'^\s*'+label+r'\s*[:：]\s*(\S.*)$',line,re.I)
                    if match:
                        add(role,match.group(1).strip(),e,line)

    edges = []
    # A literal stated charging relation supports both financial direction and roles.
    if charge.direction=='ISSUER_CHARGES_RECIPIENT' and charge.issuer and charge.recipient and charge.direction_quote:
        for e in primary:
            quote = charge.direction_quote
            pattern = re.escape(charge.issuer)+r'\s+charges\s+'+re.escape(charge.recipient)+r'(?![\w])'
            if quote in e.text and re.search(pattern,quote,re.I):
                add('issuer',charge.issuer,e,quote);add('recipient',charge.recipient,e,quote)
                edges.append({'creditor':charge.issuer,'debtor':charge.recipient,
                    'evidence_ids':[e.id],'quote':quote,'rule':'explicit_charging_statement'})

    # Email shorthand is recorded literally. No guessing SHA/TAR company expansions.
    for e in primary:
        match = re.match(r'^\s*(?P<payer>[^:：\n]{1,100}?)\s+pays?\s+(?P<payee>[^:：\n]{1,100})\s*[:：]',e.text,re.I)
        from .engine import numeric_quote_supports
        if match and numeric_quote_supports(e.text,charge.amount):
            payer,payee = match.group('payer').strip(),match.group('payee').strip()
            add('payer',payer,e,e.text);add('payee',payee,e,e.text)
            edges.append({'creditor':payee,'debtor':payer,'evidence_ids':[e.id],
                'quote':e.text,'rule':'explicit_payment_statement'})

    def unique(role):
        selected = [f for f in facts if f['role']==role]
        values = {key(f['canonical_entity']):f['canonical_entity'] for f in selected}
        if len(values)>1:
            reasons.append(f'Conflicting {role} identities')
        return next(iter(values.values())) if len(values)==1 else None

    roles = {role:unique(role) for role in ['issuer','recipient','payer','payee','beneficiary']}
    # Explicit invoice roles establish a financial direction only for a source-verified
    # ordinary invoice/debit note. Shipper/consignee/notify are never substituted.
    if not edges and roles['issuer'] and roles['recipient']:
        supporting = [f for f in facts if f['role'] in {'issuer','recipient'}]
        context = '\n'.join(e.text for e in scope_evidence)
        if re.search(r'\b(?:INVOICE|DEBIT\s+NOTE)\b',context,re.I):
            edges.append({'creditor':roles['issuer'],'debtor':roles['recipient'],
                'evidence_ids':[f['evidence_id'] for f in supporting],
                'quote':'Explicit invoice issuer and billed-to roles', 'rule':'explicit_invoice_parties'})

    header = [f for f in facts if f['role']=='header_party']
    receivers = [f for f in facts if f['role'] in {'beneficiary','payee'}]
    if header and receivers and not any(registry.same(h['entity'],p['entity']) for h in header for p in receivers):
        reasons.append('Letterhead and payment recipient differ; agency/collection relationship is unconfirmed')
    posting = None
    financial_edge = None
    pairs = {(key(registry.canonical(e['creditor'])),key(registry.canonical(e['debtor']))) for e in edges}
    if len(pairs)>1:
        reasons.append('Conflicting financial directions')
    elif len(pairs)==1:
        financial_edge = edges[0]
        creditor,debtor = financial_edge['creditor'],financial_edge['debtor']
        if registry.same(creditor,debtor):
            reasons.append('Creditor and debtor resolve to the same entity')
        elif registry.same(accounting_entity,creditor):
            posting = 'AR'
        elif registry.same(accounting_entity,debtor):
            posting = 'AP'
        else:
            reasons.append('Current accounting entity is neither confirmed creditor nor debtor')
    else:
        reasons.append('No complete evidence-backed creditor/debtor relationship')
    if charge.conflict_evidence_ids:
        reasons.append('Charge applicability is disputed by source evidence')
    context = '\n'.join(e.text for e in scope_evidence)
    if (charge.document_type and 'credit' in charge.document_type.casefold()) or re.search(r'\bCREDIT\s+NOTE\b',context,re.I):
        reasons.append('Credit note requires original bill and adjustment policy review')
    if reasons:
        posting = None
    return {'accounting_entity':accounting_entity,'canonical_accounting_entity':registry.canonical(accounting_entity),
        'posting_type':posting,'status':'RESOLVED' if posting else 'PENDING_REVIEW',
        'transaction_party':roles,'role_candidates':facts,'direction_evidence':edges,
        'review_reasons':list(dict.fromkeys(reasons))}


def enrich_parties(extraction, evidence, accounting_entity, aliases=None):
    result = extraction.model_copy(deep=True)
    for charge in result.charges:
        assessment = assess(charge,evidence,accounting_entity,aliases)
        charge.issuer = assessment['transaction_party']['issuer']
        charge.recipient = assessment['transaction_party']['recipient']
        charge.party_evidence_ids = list(dict.fromkeys(f['evidence_id'] for f in assessment['role_candidates']))
    return result


def inherit_existing(financial,old,charge,registry):
    """Unmentioned financial fields may inherit a consistent, uniquely matched ledger record."""
    if charge.intent!='MODIFY' or not old:
        return financial
    if financial['posting_type']:
        return financial
    allowed={'No complete evidence-backed creditor/debtor relationship'}
    if any(reason not in allowed for reason in financial['review_reasons']):
        return financial
    parties=old.get('transaction_party',{})
    attrs=old.get('financial_attributes',{})
    issuer,recipient=parties.get('issuer'),parties.get('recipient')
    if not issuer or not recipient or registry.same(issuer,recipient):
        return financial
    expected='AR' if registry.same(financial['accounting_entity'],issuer) else 'AP' if registry.same(financial['accounting_entity'],recipient) else None
    if not expected or attrs.get('posting_type')!=expected or not registry.same(attrs.get('accounting_entity'),financial['accounting_entity']):
        return financial
    if 'credit' in (old.get('document_info',{}).get('document_type') or '').casefold():
        return financial
    for role in ['issuer','recipient']:
        current=financial['transaction_party'].get(role)
        if current and not registry.same(current,parties[role]):
            return financial
    from copy import deepcopy
    result=deepcopy(financial)
    result.update(posting_type=expected,status='RESOLVED_INHERITED',review_reasons=[],
                  inheritance={'bill_id':old.get('id'),'basis':'Unique same-entity existing ledger record; financial fields not modified'})
    result['transaction_party']=deepcopy(parties)
    return result
