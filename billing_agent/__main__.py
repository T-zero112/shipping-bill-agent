import argparse
import json
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path
from .models import Extraction
from .parsers import Parser
from .engine import process_batch
from .normalization import normalize_extraction
from .financial import EntityRegistry, enrich_parties
from .contracts import validate_results, ADAPTER, OutputContractError
from .reporting import summarize


def dump(path, value):
    # Internal Decimal arithmetic; output contract requires JSON number.
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False,
        default=lambda x: float(x) if isinstance(x, Decimal) else str(x)), encoding='utf-8')


def read(path, default):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if path else default


def _main():
    ap = argparse.ArgumentParser(description='Shipping bill evidence pipeline; never posts to finance systems')
    ap.add_argument('--input', required=True)
    ap.add_argument('--entity', required=True)
    ap.add_argument('--output', default='outputs/latest')
    ap.add_argument('--existing')
    ap.add_argument('--entity-aliases', help='Confirmed canonical entity -> aliases JSON; no inferred abbreviations')
    ap.add_argument('--state-db', help='Opt-in LOCAL simulation ledger: apply validated CREATE/UPDATE atomically, with replay journal')
    ap.add_argument('--extractor', choices=['none', 'replay', 'llm', 'remote'], default='none')
    ap.add_argument('--replay', help='Explicit extraction fixture for offline harness tests, NOT model output')
    ap.add_argument('--pdf-mode', choices=['local', 'ocr'], default='ocr')
    ap.add_argument('--image-mode',choices=['local','remote'],default='remote')
    ap.add_argument('--local-ocr-cache',help='Optional local OCR cache directory; never used as remote OCR evidence')
    ap.add_argument('--url-map', help='SHA256 -> accessible URL; supplying this enables OCR network requests')
    ap.add_argument('--ocr-cache', help='SHA256 -> original OCR JSON response')
    ap.add_argument('--extraction-cache',help='Opt-in local model block cache; content/model/prompt/schema bound')
    ap.add_argument('--temporary-url-file',action='append',default=[],help='Explicit PDF/PNG/JPEG/TIFF file allowlist for public temporary URLs; repeat per authorized file')
    ap.add_argument('--cloudflared',help='Existing portable cloudflared executable; no system service installed')
    args = ap.parse_args()
    if args.extraction_cache:
        os.environ['BILLING_EXTRACTION_CACHE']=str(Path(args.extraction_cache).resolve())
    if not args.entity.strip():
        raise ValueError('Current accounting entity must not be empty')
    entity_aliases = read(args.entity_aliases, {})
    EntityRegistry(entity_aliases)  # Reject ambiguous configuration before model/network calls.
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for name in ['output-validation-errors.json','ledger-after.json']:
        (output/name).unlink(missing_ok=True)
    temporary_url=None
    if args.temporary_url_file:
        if not args.cloudflared:
            raise ValueError('--cloudflared required with --temporary-url-file')
        from .temporary_url import TemporaryURL
        temporary_url=TemporaryURL(args.cloudflared,args.temporary_url_file,output/'temporary-url')
    parser = Parser(args.pdf_mode, read(args.url_map, {}), read(args.ocr_cache, {}),args.image_mode,args.local_ocr_cache,temporary_url)
    source = Path(args.input)
    files = sorted(source.iterdir()) if source.is_dir() else [source]
    if not source.exists():
        parser.issues.append({'source':source.name,'error':'Input path does not exist'})
    elif source.is_dir() and not any(p.is_file() for p in files):
        parser.issues.append({'source':source.name,'error':'Input directory contains no files'})
    for p in files:
        if p.is_file():
            try:
                parser.parse(p.read_bytes(), p.name)
            except OSError as exc:
                parser.issues.append({'source':p.name,'error':f'{type(exc).__name__}: {exc}'})
    dump(output/'evidence.json', [e.model_dump() for e in parser.evidence])
    dump(output/'ocr-results.json',parser.ocr_results)
    dump(output/'remote-ocr-adapter.json',parser.remote_adapter_events)
    existing = read(args.existing, [])
    if not isinstance(existing, list):
        raise ValueError('Existing bills must be an array')
    extraction = Extraction()
    extraction_error = None
    try:
        if args.extractor == 'llm':
            from .extraction import extract
            extraction = extract(parser.evidence)
        elif args.extractor == 'remote':
            from .remote_ocr import charges
            extraction=charges(parser.evidence)
            if any(not (e.structured or {}).get('remote_context_only') for e in parser.evidence):
                extraction.errors.append('remote extractor only supports adapted remote OCR evidence; use llm for other formats')
        elif args.extractor == 'replay':
            if not args.replay:
                raise ValueError('--replay is required')
            extraction = Extraction.model_validate(read(args.replay, {}))
    except Exception as exc:
        extraction_error = f'{type(exc).__name__}: {exc}'
    dump(output/'extraction-raw.json', extraction.model_dump())
    dump(output/'model-responses.json', extraction.model_responses)
    dump(output/'recovery-trace.json',extraction.recovery_trace)
    state_report=None
    results=[]
    processing_error=None
    output_error=None
    try:
        extraction = normalize_extraction(extraction, parser.evidence)
        extraction = enrich_parties(extraction,parser.evidence,args.entity,entity_aliases)
        if args.state_db:
            from .state import process_with_state
            results,state_report=process_with_state(extraction,parser.evidence,existing,args.entity,entity_aliases,args.state_db)
            dump(output/'ledger-after.json',state_report.pop('ledger'))
        else:
            results = process_batch(extraction.charges, parser.evidence, existing, args.entity, extraction.totals,entity_aliases)
        try:
            validate_results(results)
        except Exception as exc:
            output_error=f'{type(exc).__name__}: {exc}'
            dump(output/'output-validation-errors.json',{'error':output_error})
            results=[]  # Never publish invalid business JSON as an accepted result.
    except OutputContractError as exc:
        output_error=f'{type(exc).__name__}: {exc}'
        dump(output/'output-validation-errors.json',{'error':output_error})
        results=[]
    except Exception as exc:
        processing_error=f'{type(exc).__name__}: {exc}'
        results=[]
    dump(output/'result.schema.json',ADAPTER.json_schema())
    dump(output/'financial-assessments.json',[r['audit_trail'].get('financial_assessment') for r in results])
    dump(output/'extraction.json', extraction.model_dump())
    dump(output/'result.json', results)
    report = {'mode': args.extractor, 'pdf_mode': args.pdf_mode,
        'image_mode':args.image_mode,'local_ocr_image_count':sum(not r.get('source_kind') for r in parser.ocr_results),
        'local_pdf_ocr_region_count':sum(bool(r.get('source_kind')) for r in parser.ocr_results),
        'accounting_entity': args.entity,
        'model_provider': os.environ.get('BILLING_PROVIDER', 'ollama') if args.extractor == 'llm' else None,
        'model': os.environ.get('BILLING_MODEL', 'qwen3:8b') if args.extractor == 'llm' else None,
        'evidence_count': len(parser.evidence), 'charge_count': len(extraction.charges),
        'remote_ocr_adapter_event_count':len(parser.remote_adapter_events),
        'amendment_count': sum(c.intent=='MODIFY' for c in extraction.charges),
        'successful_local_model_calls': sum(t.get('status')=='success' for t in extraction.recovery_trace) if args.extractor=='llm' and os.environ.get('BILLING_PROVIDER','ollama')=='ollama' else None,
        'validated_model_block_count':len(extraction.model_responses),
        'actual_local_model_attempts':sum(1 for t in extraction.recovery_trace if t.get('attempt',0)>0),
        'model_cache_hits':sum(t.get('status')=='success_cache_hit' for t in extraction.recovery_trace),
        'model_cache_write_error_count':sum(t.get('status')=='cache_write_failed' for t in extraction.recovery_trace),
        'operations': dict(Counter(r['operation'] for r in results)),
        'issues': parser.issues, 'extraction_warnings': extraction.warnings,
        'extraction_error': extraction_error,
        'extraction_errors': extraction.errors,
        'excluded_candidate_count': len(extraction.excluded_candidates),
        'normalization_event_count': len(extraction.normalization_events),
        'state':state_report,
        'verification': 'offline fixture replay; not LLM verification' if args.extractor == 'replay' else args.extractor}
    report.update(summarize(parser,extraction,results,args.extractor,extraction_error,output_error,processing_error))
    report['exit_code']=2 if report['status'] in {'OUTPUT_INVALID','PROCESSING_FAILED','PARTIAL_FAILURE','EXTRACTION_FAILED','PARSE_FAILED','UNRESOLVED'} else 0
    dump(output/'run_report.json', report)
    print(json.dumps({k:v for k,v in report.items() if k not in ['issues']}, ensure_ascii=False))
    return report['exit_code']


def main():
    # Initialization/input errors must also produce a machine-readable failure.
    import sys
    try:
        return _main()
    except (ValueError,OSError) as exc:
        destination='outputs/latest'
        for index,arg in enumerate(sys.argv):
            if arg=='--output' and index+1<len(sys.argv):destination=sys.argv[index+1]
            elif arg.startswith('--output='):destination=arg.split('=',1)[1]
        output=Path(destination)
        output.mkdir(parents=True,exist_ok=True)
        dump(output/'result.json',[])
        dump(output/'result.schema.json',ADAPTER.json_schema())
        report={'status':'INPUT_INVALID','exit_code':2,'operational_complete':False,
                'output_validation':{'passed':False,'error':'Business processing did not complete'},
                'error':f'{type(exc).__name__}: {exc}','files':[],
                'empty_result_confirmed_no_bill':False}
        dump(output/'run_report.json',report)
        print(json.dumps(report,ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
