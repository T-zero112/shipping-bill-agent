"""Bounded, resumable local model work; cache stores only validated leaf results."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import urllib.error

VERSION='line-owned-document-blocks-v1'


class Truncated(ValueError):pass


@dataclass
class Block:
    start:int
    end:int
    text:str
    context:str=''


def split_lines(block):
    lines=block.text.splitlines(keepends=True)
    if len(lines)<2:return []
    boundary=min(range(1,len(lines)),key=lambda i:abs(sum(map(len,lines[:i]))-len(block.text)/2))
    left=''.join(lines[:boundary]);right=''.join(lines[boundary:])
    return [Block(block.start,block.start+len(left),left,block.context),
            Block(block.start+len(left),block.end,right,block.context)]


def blocks(text,limit):
    """Never split a physical row; start new context at explicit document headers."""
    result=[];offset=0;start=0;parts=[];headers=[];seen_identity=False
    identity=re.compile(r'^\s*(?:INVOICE|CREDIT NOTE|DEBIT NOTE|D/C)\s*(?:NO\.?|NUMBER|#)\s*[:：]',re.I)
    header=re.compile(r'^\s*(?:INVOICE|CREDIT NOTE|DEBIT NOTE|D/C|CURRENCY|CURR|CCY|DESCRIPTION\b|ISSUER\s*:|BILL TO\s*:)',re.I)
    for line in text.splitlines(keepends=True):
        if identity.match(line) and parts and seen_identity:
            result.append(Block(start,offset,''.join(parts),'\n'.join(headers)))
            parts=[];headers=[];start=offset
        if identity.match(line):seen_identity=True
        if parts and sum(map(len,parts))+len(line)>limit:
            result.append(Block(start,offset,''.join(parts),'\n'.join(headers)))
            parts=[];start=offset
        parts.append(line);offset+=len(line)
        if header.match(line) and len(line)<300:
            headers.append(line.strip())
            headers=headers[-6:]
    if parts:result.append(Block(start,offset,''.join(parts),'\n'.join(headers)))
    return result


def exact_quote(quote,text):
    if quote and quote in text:return True
    pattern=r'\s+'.join(re.escape(s) for s in quote.split())
    return bool(pattern and re.search(pattern,text))


class Recovery:
    def __init__(self,cache_dir=None,limit=10000,attempts=3,max_depth=3,max_calls=32,backoff=.2):
        self.cache=Path(cache_dir) if cache_dir else None
        self.limit=limit;self.attempts=attempts;self.max_depth=max_depth;self.max_calls=max_calls;self.backoff=backoff
        self.trace=[];self.failures=[];self.calls=0

    def read(self,key):
        path=self.cache/(key+'.json') if self.cache else None
        try:
            if path and path.exists():return json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,OSError):self.trace.append({'block_id':key,'status':'corrupt_cache_ignored'})

    def write(self,key,data):
        if not self.cache:return
        try:
            self.cache.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.cache,delete=False,suffix='.tmp') as f:
                json.dump(data,f,ensure_ascii=False,allow_nan=False);temp=Path(f.name)
            temp.replace(self.cache/(key+'.json'))
        except OSError as exc:
            self.trace.append({'block_id':key,'status':'cache_write_failed','error':str(exc)})

    def run(self,evidence,fingerprint,invoke,validate):
        text=evidence.text
        origin=hashlib.sha256((VERSION+fingerprint+evidence.id+text).encode()).hexdigest()
        output=[]
        def visit(block,depth=0):
            key=hashlib.sha256(f'{origin}:{block.start}:{block.end}:{block.context}'.encode()).hexdigest()
            event={'block_id':key,'evidence_id':evidence.id,'source':evidence.source,'location':evidence.location,
                   'start':block.start,'end':block.end,'depth':depth}
            saved=self.read(key)
            if saved and saved.get('type')=='split' and depth<self.max_depth:
                children=split_lines(block)
                if children:
                    self.trace.append({**event,'status':'split_plan_cache_hit'})
                    for child in children:visit(child,depth+1)
                    return
            def check(raw):
                page=validate(raw)
                for row in page.rows:
                    if not exact_quote(row.quote,block.text):raise ValueError('Fee quote must belong to owned block, not repeated context')
                for total in page.totals:
                    if not exact_quote(total.quote,block.text):raise ValueError('Total quote must belong to owned block')
                return page
            if saved and saved.get('type')=='success':
                try:
                    page=check(saved['response'])
                    output.append({'page':page,'block':block,'block_id':key,'cache_hit':True})
                    self.trace.append({**event,'status':'success_cache_hit','attempt':0});return
                except (ValueError,KeyError,TypeError):
                    self.trace.append({**event,'status':'invalid_cache_ignored'})
            error='';truncated=False
            if len(block.text)>self.limit:
                error='Physical row exceeds configured block budget; not silently truncated'
            else:
                for attempt in range(1,self.attempts+1):
                    if self.calls>=self.max_calls:
                        error='Per-evidence call budget exhausted';break
                    try:
                        self.calls+=1
                        raw=invoke(block,error)
                        page=check(raw)
                        self.write(key,{'type':'success','response':page.model_dump(mode='json')})
                        output.append({'page':page,'block':block,'block_id':key,'cache_hit':False})
                        self.trace.append({**event,'status':'success','attempt':attempt});return
                    except Truncated as exc:
                        error=str(exc);truncated=True
                        self.trace.append({**event,'status':'truncated','attempt':attempt,'error':error});break
                    except Exception as exc:
                        error=f'{type(exc).__name__}: {exc}'
                        self.trace.append({**event,'status':'retryable_failure','attempt':attempt,'error':error})
                        if isinstance(exc,urllib.error.HTTPError) and exc.code not in {408,429,500,502,503,504}:break
                        if attempt<self.attempts:time.sleep(self.backoff*2**(attempt-1))
            children=split_lines(block) if truncated and depth<self.max_depth else []
            if children:
                self.write(key,{'type':'split'})
                for child in children:visit(child,depth+1)
            else:
                self.failures.append({**event,'status':'failed','error':error})
                self.trace.append(self.failures[-1])
        for block in blocks(text,self.limit):visit(block)
        return output
