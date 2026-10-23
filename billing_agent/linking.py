"""Recompute document scope from source evidence, never from model-supplied links."""
import re

REFERENCE=re.compile(r'(?im)(?<!\w)(?:INVOICE|DEBIT\s+NOTE|CREDIT\s+NOTE|D/C|D/N)\s*(?:NO\.?|NUMBER|#)\s*[:：]?\s*([A-Z0-9][A-Z0-9_./-]*\d[A-Z0-9_./-]*)')


def page_key(e):
    m=re.match(r'page:\d+(?:/|$)',e.location)
    return (e.source,e.location.split('/')[0]) if m else None


def page_groups(evidence):
    pages={page_key(e):e for e in evidence if re.fullmatch(r'page:\d+',e.location)}
    identities={}
    rejected={}
    groups={}
    for key,page in pages.items():
        refs={ref for line in page.text.upper().splitlines()
              if not re.match(r'\s*(?:ORIGINAL|RELATED|AGAINST|原单|原发票)',line)
              for ref in REFERENCE.findall(line)}
        if len(refs)==1:
            identity=next(iter(refs))
            identities[key]=identity
            groups.setdefault((key[0],identity),[]).append(key)
        elif len(refs)>1:
            rejected[key]='Multiple document references on one page; page association disabled'
    from .business import facts,LABELS,date_value
    for group in groups.values():
        dates={v for k in group for v,_ in facts(pages[k].text,'document_date')}
        raw_dates={date_value(m.group(1)) or m.group(1) for k in group for m in re.finditer(
            LABELS['document_date']+r'\s*[:：]?\s*(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4})',pages[k].text,re.I)}
        roles={role:{m.group(1).strip().casefold() for k in group for m in re.finditer(
            r'(?im)^\s*'+label+r'\s*[:：]\s*([^\n]+)',pages[k].text)}
            for role,label in [('issuer','(?:ISSUER|ISSUED BY)'),('recipient','(?:BILL TO|BILLED TO)')]}
        types={m.group(1).upper() for k in group for m in re.finditer(r'^\s*(CREDIT NOTE|DEBIT NOTE|INVOICE)\b',pages[k].text,re.I|re.M)}
        if len(dates)>1 or len(raw_dates)>1 or any(len(v)>1 for v in roles.values()) or ('CREDIT NOTE' in types and len(types)>1):
            for k in group:rejected[k]='Conflicting document dates, parties or types; page association disabled'
    return pages,identities,groups,rejected


def linked_scope(charge,evidence):
    refs={e.id:e for e in evidence}
    primary=[refs[i] for i in charge.evidence_ids if i in refs]
    selected={e.id for e in primary}
    selected|={e.parent_id for e in primary if e.parent_id in refs}
    pages,identities,groups,rejected=page_groups(evidence)
    keys={page_key(e) for e in primary if page_key(e)}
    # A candidate already bound to multiple pages/files cannot enlarge its scope.
    if len(keys)==1 and len({e.source for e in primary})==1:
        key=next(iter(keys))
        identity=identities.get(key)
        if identity and key not in rejected and (not charge.reference_no or charge.reference_no.upper()==identity):
            group=groups[(key[0],identity)]
            if not any(k in rejected for k in group):
                selected|={e.id for e in evidence if page_key(e) in group}
    return [e for e in evidence if e.id in selected]


def association(charge,evidence):
    pages,identities,groups,rejected=page_groups(evidence)
    primary=[e for e in evidence if e.id in charge.evidence_ids]
    keys={page_key(e) for e in primary if page_key(e)}
    reasons=[rejected[k] for k in keys if k in rejected]
    refs={identities[k] for k in keys if k in identities}
    if len(refs)>1 or len({e.source for e in primary})>1:
        reasons.append('Charge spans different document identities or sources')
    if charge.reference_no and refs and refs!={charge.reference_no.upper()}:
        reasons.append('Extracted reference conflicts with explicit page identity')
    return {'rule':'same source and unique explicit document reference; reject date/party/type conflicts',
            'reference_no':next(iter(refs)) if len(refs)==1 and not reasons else None,
            'evidence_ids':[e.id for e in linked_scope(charge,evidence)],
            'review_reasons':list(dict.fromkeys(reasons))}


def transport_facts(e):
    patterns=[('master_bl',r'(?:MASTER\s+B/L|M\s*B/L|MBL|主提单号)'),
              ('house_bl',r'(?:HOUSE\s+B/L|H\s*B/L|HBL|分提单号)'),
              ('bl',r'(?:B/L|BILL\s+OF\s+LADING|提单号)')]
    output=[]
    for line in e.text.splitlines():
        selected=None
        for kind,label in patterns:
            match=re.search(r'(?<!\w)'+label+r'(?:\s+(?:NO\.?|NUMBER))?\s*[:：#]?\s*([A-Z0-9][A-Z0-9_/-]*\d[A-Z0-9_/-]*)',line,re.I)
            if match:
                selected={'kind':kind,'value':match.group(1).upper(),'evidence_id':e.id,'quote':match.group(0)}
                output.append(selected)
                break
        match=re.search(r'(?<!\w)(?:CONTAINER|CNTR\.?|箱号)(?:\s+(?:NO\.?|NOS\.?|NUMBER))?\s*[:：#]?\s*([A-Z]{4}\d{7}(?:\s*[,;/]\s*[A-Z]{4}\d{7})*)',line,re.I)
        if match:
            for value in re.findall(r'[A-Z]{4}\d{7}',match.group(1).upper()):
                output.append({'kind':'container','value':value,'evidence_id':e.id,'quote':match.group(0)})
    return output


def enrich_links(extraction,evidence):
    for c in extraction.charges:
        from .credits import enrich_credit_identity
        enrich_credit_identity(c,evidence)
        link=association(c,evidence)
        for reason in link['review_reasons']:
            if reason not in c.warnings:c.warnings.append(reason)
        if c.intent=='MODIFY':continue
        if not c.reference_no and link['reference_no']:c.reference_no=link['reference_no']
        found=allowed_transport(c,evidence)
        # Identical primary/parent quotes can repeat; retain distinct values and proofs.
        c.transport_identifiers=list({(f['kind'],f['value'],f['evidence_id']):f for f in found}.values())
    return extraction


def allowed_transport(c,evidence):
    found=[fact for e in linked_scope(c,evidence) for fact in transport_facts(e)]
    primary_rows=[e for e in evidence if e.id in c.evidence_ids and
                  ('/charge-row:' in e.location or (e.structured or {}).get('kind') in {'spreadsheet_row','word_table_row'})]
    specific=[f for e in primary_rows for f in transport_facts(e)]
    kinds={f['kind'] for f in specific}
    return [f for f in found if f['kind'] not in kinds]+specific


def validate_transport(c,evidence):
    allowed=allowed_transport(c,evidence)
    errors=[]
    valid=[]
    for f in c.transport_identifiers:
        if f in allowed:valid.append(f)
        else:errors.append('Transport identifier lacks scoped exact evidence; excluded from matching')
    c.transport_identifiers=valid
    return errors
