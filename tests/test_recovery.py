import json
from collections import Counter
from pydantic import BaseModel
import pytest
from billing_agent.models import Evidence
from billing_agent.recovery import Recovery,Truncated,blocks


class Row(BaseModel):
    quote:str


class Page(BaseModel):
    rows:list[Row]
    totals:list[Row]=[]


def ev(text):return Evidence(id='source',source='synthetic.txt',location='text',text=text)
def valid(block):return {'rows':[{'quote':line} for line in block.text.splitlines() if line.startswith('Fee')]}


def test_structural_blocks_preserve_all_lines_and_document_boundaries():
    text='INVOICE No: INV1\nCURRENCY: USD\nFee A 10\nFee B 20\nINVOICE No: INV2\nCURRENCY: EUR\nFee C 30\n'
    work=blocks(text,45)
    assert ''.join(b.text for b in work)==text
    for b in work:
        assert len(b.text)<=45 and text[b.start:b.end]==b.text
        if 'INV2' in b.text or 'Fee C' in b.text:assert 'INV1' not in b.context


def test_transient_timeout_and_bad_json_retry_with_validation_feedback(tmp_path):
    calls=[]
    def invoke(block,feedback):
        calls.append(feedback)
        if len(calls)==1:raise TimeoutError('Injected timeout')
        if len(calls)==2:return {'not_rows':[]}
        return valid(block)
    r=Recovery(tmp_path,attempts=3,backoff=0)
    out=r.run(ev('Fee A USD 10'),'model-v1',invoke,Page.model_validate)
    assert len(out)==1 and len(calls)==3 and calls[1] and calls[2] and not r.failures


def test_partial_failure_resume_only_calls_failed_block(tmp_path):
    evidence=ev('Fee A USD 10\nFee B USD 20\n')
    calls=Counter()
    def broken(block,feedback):
        calls[block.text]+=1
        if 'Fee B' in block.text:raise TimeoutError('Injected permanent failure')
        return valid(block)
    first=Recovery(tmp_path,limit=14,attempts=2,backoff=0)
    out=first.run(evidence,'v1',broken,Page.model_validate)
    assert len(out)==1 and len(first.failures)==1
    assert calls['Fee A USD 10\n']==1 and calls['Fee B USD 20\n']==2
    resumed=[]
    def fixed(block,feedback):resumed.append(block.text);return valid(block)
    second=Recovery(tmp_path,limit=14,attempts=2,backoff=0)
    out=second.run(evidence,'v1',fixed,Page.model_validate)
    assert resumed==['Fee B USD 20\n']
    assert len(out)==2 and sum(o['cache_hit'] for o in out)==1 and not second.failures
    assert len([r for o in out for r in o['page'].rows])==2


def test_truncated_parent_plan_is_resumed_without_recalling_parent(tmp_path):
    evidence=ev('Fee A 10\nFee B 20\n')
    calls=[]
    def first_call(block,feedback):
        calls.append(block.text)
        if 'Fee A' in block.text and 'Fee B' in block.text:raise Truncated('Injected length')
        if 'Fee B' in block.text:raise TimeoutError('Injected leaf failure')
        return valid(block)
    r=Recovery(tmp_path,attempts=1,backoff=0)
    assert len(r.run(evidence,'v1',first_call,Page.model_validate))==1
    again=[]
    def fixed(block,feedback):again.append(block.text);return valid(block)
    r2=Recovery(tmp_path,attempts=1,backoff=0)
    assert len(r2.run(evidence,'v1',fixed,Page.model_validate))==2
    assert again==['Fee B 20\n'] and any(t['status']=='split_plan_cache_hit' for t in r2.trace)


@pytest.mark.parametrize('change',['model','content'])
def test_cache_invalidates_on_configuration_or_input_change(tmp_path,change):
    r=Recovery(tmp_path,backoff=0);r.run(ev('Fee A 10'),'v1',lambda b,f:valid(b),Page.model_validate)
    second=Recovery(tmp_path,backoff=0)
    second.run(ev('Fee A 11' if change=='content' else 'Fee A 10'),'v2' if change=='model' else 'v1',lambda b,f:valid(b),Page.model_validate)
    assert second.calls==1


def test_repeated_context_cannot_own_a_fee_quote(tmp_path):
    r=Recovery(tmp_path,limit=15,attempts=1,backoff=0)
    out=r.run(ev('Fee A USD 10\nFee B USD 20\n'),'v1',lambda b,f:{'rows':[{'quote':'Fee A USD 10'}]},Page.model_validate)
    assert len(out)==1 and len(r.failures)==1


def test_oversized_atomic_row_never_silently_slices_amounts():
    r=Recovery(limit=10,backoff=0)
    assert not r.run(ev('Fee A USD 100000000000000'),'v1',lambda b,f:valid(b),Page.model_validate)
    assert r.calls==0 and r.failures


def test_call_budget_and_split_depth_are_finite(tmp_path):
    r=Recovery(tmp_path,max_calls=2,max_depth=2,attempts=1,backoff=0)
    def truncate(block,feedback):raise Truncated('Injected length')
    r.run(ev('Fee A\nFee B\nFee C\nFee D\n'),'v1',truncate,Page.model_validate)
    assert r.calls==2 and r.failures


def test_corrupt_cache_recomputed(tmp_path):
    evidence=ev('Fee A 10')
    r=Recovery(tmp_path,backoff=0);r.run(evidence,'v1',lambda b,f:valid(b),Page.model_validate)
    next(tmp_path.glob('*.json')).write_text('broken',encoding='utf-8')
    r2=Recovery(tmp_path,backoff=0);r2.run(evidence,'v1',lambda b,f:valid(b),Page.model_validate)
    assert r2.calls==1 and not r2.failures


def test_cache_write_failure_does_not_retry_successful_model(tmp_path):
    bad=tmp_path/'file';bad.write_text('not a directory')
    r=Recovery(bad,backoff=0)
    out=r.run(ev('Fee A 10'),'v1',lambda b,f:valid(b),Page.model_validate)
    assert len(out)==1 and r.calls==1 and not r.failures
    assert any(t['status']=='cache_write_failed' for t in r.trace)


def test_extractor_integration_resumes_failed_evidence_only(tmp_path,monkeypatch):
    import io
    import urllib.request
    from billing_agent.extraction import extract_local
    from billing_agent.engine import process_batch
    from billing_agent.reporting import summarize
    from billing_agent.parsers import Parser
    monkeypatch.setenv('BILLING_EXTRACTION_CACHE',str(tmp_path))
    monkeypatch.setattr('billing_agent.recovery.time.sleep',lambda _:None)
    class Fake:
        broken=True
        calls=[]
        def open(self,request,timeout):
            content=json.loads(request.data)['messages'][-1]['content']
            self.calls.append(content)
            second='Doc Fee' in content
            if second and self.broken:raise TimeoutError('Injected disconnected model')
            page={'reference_no':'INV2' if second else 'INV1','document_type':'INVOICE','totals':[],
                  'rows':[{'charge_item':'Doc Fee' if second else 'Handling Fee','amount':20 if second else 10,
                           'currency':'USD','quote':'Doc Fee USD 20' if second else 'Handling Fee USD 10'}]}
            return io.BytesIO(json.dumps({'done_reason':'stop','message':{'content':json.dumps(page)}}).encode())
    fake=Fake();monkeypatch.setattr(urllib.request,'build_opener',lambda *args:fake)
    a=Evidence(id='a',source='a.txt',location='text',text='Invoice No: INV1\nHandling Fee USD 10')
    b=Evidence(id='b',source='b.txt',location='text',text='Invoice No: INV2\nDoc Fee USD 20')
    first=extract_local([a,b])
    assert len(first.charges)==1 and len(first.errors)==1 and len(fake.calls)==4
    parser=Parser();parser.evidence=[a,b]
    assert summarize(parser,first,process_batch(first.charges,[a,b],[],'Test'),'llm')['status']=='PARTIAL_FAILURE'
    fake.broken=False;fake.calls=[]
    second=extract_local([a,b])
    assert len(second.charges)==2 and not second.errors and len(fake.calls)==1
    assert any(t['status']=='success_cache_hit' for t in second.recovery_trace)
