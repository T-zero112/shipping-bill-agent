from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest
from pydantic import ValidationError
from billing_agent.contracts import validate_results
from billing_agent.models import Charge, Evidence, Extraction
from billing_agent.engine import process
from billing_agent.parsers import Parser
from billing_agent.reporting import summarize


def record():
    ev=Evidence(id='e',source='bill.txt',location='row',text='A charges B. DN1 Handling Fee USD 40')
    c=Charge(reference_no='DN1',charge_item='Handling Fee',amount=40,currency='USD',issuer='A',recipient='B',
             evidence_ids=['e'],amount_quote='USD 40',direction='ISSUER_CHARGES_RECIPIENT',direction_quote='A charges B.')
    return process(c,[ev],[],'B')


@pytest.mark.parametrize('path,value',[
    (('operation',),'WRONG'),(('charge_details','amount'),'40'),(('charge_details','amount'),True),
    (('charge_details','amount'),float('nan')),(('financial_attributes','posting_type'),None),
    (('charge_details','currency'),'ZZZ'),(('audit_trail','review_reasons'),['unresolved']),
])
def test_invalid_final_output_is_rejected(path,value):
    r=record();target=r
    for key in path[:-1]:target=target[key]
    target[path[-1]]=value
    with pytest.raises((ValueError,ValidationError)):validate_results([r])


def test_output_keeps_inherited_extensions_and_legal_nulls():
    r=record();r['custom_ledger_note']='preserve'
    before=deepcopy(r)
    assert validate_results([r])==[before]
    r['operation']='PENDING_REVIEW';r['charge_details']['amount']=None
    r['audit_trail']['review_reasons']=['Amount missing']
    validate_results([r])


@pytest.mark.parametrize('kind,expected',[
    ('empty','EMPTY_RESULT'),('none','NOT_EXTRACTED'),('parse','PARSE_FAILED'),
    ('extract','EXTRACTION_FAILED'),('partial','PARTIAL_FAILURE'),('review','PENDING_REVIEW'),
    ('success','SUCCESS'),('output','OUTPUT_INVALID'),('processing','PROCESSING_FAILED')])
def test_distinct_statuses(kind,expected):
    p=Parser();p.parse(b'Hello','input.txt');ext=Extraction();results=[]
    error=output=processing=None;mode='llm'
    if kind=='none':mode='none'
    if kind in {'parse','partial'}:p.issues.append({'source':'bad.doc','error':'Invalid file'})
    if kind=='extract':error='Invalid model JSON'
    if kind in {'partial','review','success'}:results=[record()]
    if kind=='review':results[0]['operation']='PENDING_REVIEW';results[0]['audit_trail']['review_reasons']=['Needs review']
    if kind=='output':output='Invalid output schema'
    if kind=='processing':processing='Engine failed'
    report=summarize(p,ext,results,mode,error,output,processing)
    assert report['status']==expected
    assert not report['empty_result_confirmed_no_bill']


def test_remote_ocr_success_empty_is_unresolved_and_failure_is_error():
    data=b'synthetic image';key=hashlib.sha256(data).hexdigest()
    p=Parser(image_mode='remote',ocr_cache={key:{'success':True,'data':{'list':[],'pages_split':[],'orderType':'None'}}})
    p.parse(data,'synthetic.png')
    report=summarize(p,Extraction(),[],'llm')
    assert report['status']=='UNRESOLVED' and report['files'][0]['status']=='UNRESOLVED'
    assert report['designated_ocr_verified'] is False
    p=Parser(ocr_cache={key:{'success':False,'data':{},'error_code':'FAIL'}})
    p.parse(data,'synthetic.png')
    assert summarize(p,Extraction(),[],'llm')['status']=='PARSE_FAILED'


def test_cli_missing_input_writes_report_and_nonzero_exit(tmp_path):
    output=tmp_path/'output'
    run=subprocess.run([sys.executable,'-m','billing_agent','--input',str(tmp_path/'missing'),
                        '--entity','Test','--extractor','none','--output',str(output)],capture_output=True)
    assert run.returncode==2
    report=json.loads((output/'run_report.json').read_text(encoding='utf-8'))
    assert report['status']=='PARSE_FAILED'
    assert (output/'result.schema.json').exists()


def test_invalid_state_output_rolls_back(tmp_path,monkeypatch):
    import sqlite3
    from billing_agent.state import process_with_state
    monkeypatch.setattr('billing_agent.contracts.validate_results',lambda rows:(_ for _ in ()).throw(ValueError('bad output')))
    path=tmp_path/'state.sqlite'
    with pytest.raises(ValueError):process_with_state(Extraction(),[],[],'Test',{},path)
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from snapshots').fetchone()[0]==0


def test_cli_invalid_output_is_not_published(tmp_path,monkeypatch):
    import billing_agent.__main__ as cli
    source=tmp_path/'input.txt';source.write_text('Hello',encoding='utf-8')
    output=tmp_path/'out'
    monkeypatch.setattr(sys,'argv',['billing_agent','--input',str(source),'--entity','B',
                                  '--output',str(output),'--extractor','none'])
    monkeypatch.setattr(cli,'process_batch',lambda *args:[{'operation':'BAD'}])
    assert cli.main()==2
    assert json.loads((output/'result.json').read_text())==[]
    report=json.loads((output/'run_report.json').read_text())
    assert report['status']=='OUTPUT_INVALID' and not report['output_validation']['passed']


def test_empty_remote_response_is_not_sent_to_model(monkeypatch):
    from billing_agent.extraction import extract_local
    data=b'image';key=hashlib.sha256(data).hexdigest()
    p=Parser(ocr_cache={key:{'success':True,'data':{'list':[],'pages_split':[]}}})
    p.parse(data,'image.png')
    monkeypatch.setattr('urllib.request.OpenerDirector.open',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Unexpected model call')))
    result=extract_local(p.evidence)
    assert not result.errors and not result.charges and result.warnings
