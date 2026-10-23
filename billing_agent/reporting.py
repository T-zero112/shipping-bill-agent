"""Operational status is separate from review decisions and acceptance warnings."""
def summarize(parser, extraction, results, mode, extraction_error=None, output_error=None, processing_error=None):
    failures=[i for i in parser.issues if 'error' in i]
    warnings=[i for i in parser.issues if 'warning' in i]
    blocked_rows=[e for e in parser.evidence if (e.structured or {}).get('review_reasons')]
    unresolved=[i for i in parser.issues if i.get('status')=='unresolved_ocr']
    partial=bool(failures or extraction_error or extraction.errors or processing_error)
    if output_error:
        status='OUTPUT_INVALID'
    elif processing_error:
        status='PROCESSING_FAILED'
    elif partial:
        status='PARTIAL_FAILURE' if results else ('EXTRACTION_FAILED' if extraction_error or extraction.errors else 'PARSE_FAILED')
    elif mode=='none':
        status='NOT_EXTRACTED'
    elif unresolved:
        status='UNRESOLVED'
    elif any(r['operation']=='PENDING_REVIEW' for r in results) or blocked_rows:
        status='PENDING_REVIEW'
    elif not results:
        status='EMPTY_RESULT'
    else:
        status='SUCCESS'
    files=[]
    for source in sorted(set(parser.sources)|{e.source for e in parser.evidence}|{i['source'] for i in parser.issues}):
        issues=[i for i in parser.issues if i['source']==source]
        evidence=[e for e in parser.evidence if e.source==source]
        candidates=[c for c in extraction.charges if set(c.evidence_ids)&{e.id for e in evidence}]
        decisions=[r for r in results if any(e.get('source')==source for e in r['audit_trail'].get('evidence',[]))]
        errors=[error for error in extraction.errors if error.startswith(source+'/')]
        if output_error and candidates:stage='OUTPUT_INVALID'
        elif processing_error and candidates:stage='PROCESSING_FAILED'
        elif any('error' in i for i in issues):stage='PARSE_FAILED'
        elif extraction_error and evidence:stage='EXTRACTION_FAILED'
        elif errors:stage='EXTRACTION_FAILED'
        elif any(i.get('status')=='unresolved_ocr' for i in issues):stage='UNRESOLVED'
        elif any(r['operation']=='PENDING_REVIEW' for r in decisions) or any((e.structured or {}).get('review_reasons') for e in evidence):stage='PENDING_REVIEW'
        elif decisions:stage='SUCCESS'
        elif any(i.get('status')=='duplicate_file' for i in issues):stage='DUPLICATE_SKIPPED'
        elif mode=='none':stage='NOT_EXTRACTED'
        else:stage='EMPTY_RESULT'
        files.append({'source':source,'status':stage,'evidence_count':len(evidence),
                      'charge_count':len(candidates),'issues':issues,'extraction_errors':errors,
                      'note':'Empty extraction is not proof that a document contains no bill' if stage=='EMPTY_RESULT' else None})
    return {'status':status,'files':files,'parse_error_count':len(failures),
            'extraction_error_count':len(extraction.errors)+(1 if extraction_error else 0),
            'processing_error':processing_error,'output_validation':{'passed':not output_error and not processing_error,'error':output_error or processing_error},
            'warning_count':len(warnings),'unresolved_ocr_count':len(unresolved),
            'empty_result_confirmed_no_bill':False,
            'has_review_items':status in {'PENDING_REVIEW','UNRESOLVED'},
            'operational_complete':status in {'SUCCESS','PENDING_REVIEW','EMPTY_RESULT'},
            'ocr_response_received':any(e.method in {'ocr_api','ocr_replay'} for e in parser.evidence),
            'remote_ocr_adapted_charge_count':sum(bool((e.structured or {}).get('remote_charge')) for e in parser.evidence),
            'remote_ocr_source_corroborated_charge_count':sum((e.structured or {}).get('reconciliation') in
                {'exact_original_rows','merged_rows_restored_from_original'} for e in parser.evidence),
            'designated_ocr_verified':False if any(e.method in {'ocr_api','ocr_replay'} or 'unverified' in e.method for e in parser.evidence) else None}
