"""Offline injected faults with persistent checkpoints, separate from model QA."""
import json
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pydantic import BaseModel
from billing_agent.models import Evidence
from billing_agent.recovery import Recovery,Truncated
from billing_agent.__main__ import dump


class Row(BaseModel):quote:str
class Page(BaseModel):
    rows:list[Row]
    totals:list[Row]=[]


def main():
    root=ROOT/'outputs/recovery/faults';root.mkdir(parents=True,exist_ok=True)
    checks=[]
    evidence=Evidence(id='synthetic',source='synthetic.txt',location='text',text='Fee A USD 10\nFee B USD 20\n')
    for split in [False,True]:
        with tempfile.TemporaryDirectory(prefix='recovery-probe-') as cache:
            def broken(block,feedback):
                if split and 'Fee A' in block.text and 'Fee B' in block.text:raise Truncated('Injected truncated output')
                if 'Fee B' in block.text:raise TimeoutError('Injected model unavailable')
                return {'rows':[{'quote':block.text.strip()}]}
            first=Recovery(cache,limit=100 if split else 14,attempts=2,backoff=0)
            a=first.run(evidence,'synthetic-config-v1',broken,Page.model_validate)
            called=[]
            def repaired(block,feedback):called.append(block.text);return {'rows':[{'quote':block.text.strip()}]}
            second=Recovery(cache,limit=100 if split else 14,attempts=2,backoff=0)
            b=second.run(evidence,'synthetic-config-v1',repaired,Page.model_validate)
            passed=len(a)==1 and len(first.failures)==1 and len(b)==2 and not second.failures and called==['Fee B USD 20\n']
            case='truncation_split_resume' if split else 'failed_block_resume'
            dump(root/(case+'.json'),{'first_trace':first.trace,'resumed_trace':second.trace,
                                     'resumed_model_inputs':called,'passed':passed})
            checks.append({'case':case,'passed':passed,'first_model_attempts':first.calls,'resume_model_attempts':second.calls})
    result={'scope':'Offline injected timeout/truncation and real filesystem cache; not a model availability test',
            'checks':checks,'passed':all(c['passed'] for c in checks)}
    dump(root/'validation.json',result);print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
