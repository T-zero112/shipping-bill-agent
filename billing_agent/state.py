"""Transactional local simulation ledger and replay journal, never a finance connector."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from decimal import Decimal
from .engine import process_batch
from .financial import EntityRegistry
from .requests import request_key


def encode(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,
        default=lambda v:float(v) if isinstance(v,Decimal) else str(v))


def ledger_row(row):
    result=deepcopy(row)
    result.pop('operation',None);result.pop('audit_trail',None)
    return result


def process_with_state(extraction,evidence,seed,entity,aliases,path):
    registry=EntityRegistry(aliases)
    scope=registry.canonical(entity).casefold()
    target=Path(path)
    target.parent.mkdir(parents=True,exist_ok=True)
    connection=sqlite3.connect(target,timeout=30,isolation_level=None)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS snapshots (scope TEXT PRIMARY KEY, bills TEXT NOT NULL, revision INTEGER NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS journal (scope TEXT NOT NULL, request_key TEXT NOT NULL, target_id TEXT NOT NULL, PRIMARY KEY(scope,request_key))')
        connection.execute('BEGIN IMMEDIATE')
        saved=connection.execute('SELECT bills,revision FROM snapshots WHERE scope=?',(scope,)).fetchone()
        if saved:
            bills=json.loads(saved[0]);revision=saved[1]
        else:
            identities=[r.get('id') for r in seed]
            if any(not i for i in identities) or len(set(identities))!=len(identities):
                raise ValueError('State seed requires nonempty unique bill IDs')
            bills=[ledger_row(r) for r in seed];revision=0
        results=process_batch(extraction.charges,evidence,bills,entity,extraction.totals,aliases)
        committed=0;replays=0
        for charge,result in zip(extraction.charges,results):
            token=request_key(charge,evidence,scope)
            result['audit_trail']['request_key']=token
            prior=connection.execute('SELECT target_id FROM journal WHERE scope=? AND request_key=?',(scope,token)).fetchone()
            if prior:
                current=next((r for r in bills if r['id']==prior[0]),None)
                if current is None:
                    result['operation']='PENDING_REVIEW'
                    result['audit_trail']['review_reasons'].append('Previously applied target is no longer present')
                    result['audit_trail']['proposed_changes']={}
                    continue
                audit=result['audit_trail']
                audit.update(proposed_changes={},idempotent_replay=True,previously_applied_target_id=prior[0])
                # Current state is authoritative, even if an old email requests a stale value.
                result.clear();result.update(deepcopy(current),operation='NO_CHANGE',audit_trail=audit)
                replays+=1
                continue
            if result['operation'] not in {'CREATE','UPDATE','NO_CHANGE'}:
                continue
            identity=None
            if result['operation']=='UPDATE':
                ids=result['audit_trail']['matched_bill_ids']
                identity=ids[0] if len(ids)==1 else None
                current=next((r for r in bills if r['id']==identity),None)
                patch=result['audit_trail']['proposed_changes']
                valid=bool(current and patch)
                for field,change in patch.items():
                    value=current['charge_details'].get(field) if current else None
                    equal=Decimal(str(value))==Decimal(str(change['before'])) if field=='amount' and value is not None else value==change['before']
                    valid &= equal and field in {'amount','currency','payment_term'}
                if not valid:
                    result['operation']='PENDING_REVIEW'
                    result['audit_trail']['review_reasons'].append('State changed before patch application')
                    continue
                for field,change in patch.items():
                    current['charge_details'][field]=change['after']
                committed+=1
            elif result['operation']=='CREATE':
                identity='bill-'+token[:24]
                if any(r['id']==identity for r in bills):
                    raise ValueError('Unexpected deterministic bill ID collision')
                result['id']=identity
                bills.append(ledger_row(result));committed+=1
            elif charge.intent=='MODIFY':
                ids=result['audit_trail']['matched_bill_ids']
                identity=ids[0] if len(ids)==1 else None
            if identity:
                connection.execute('INSERT INTO journal(scope,request_key,target_id) VALUES (?,?,?)',(scope,token,identity))
        from .contracts import validate_results
        validate_results(results)  # Validate inside transaction; invalid output must roll back writes.
        new_revision=revision+(1 if committed else 0)
        connection.execute('INSERT INTO snapshots(scope,bills,revision) VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET bills=excluded.bills,revision=excluded.revision',
                           (scope,encode(bills),new_revision))
        connection.execute('COMMIT')
        return results,{'mode':'local_simulation','database':str(target.resolve()),
            'revision_before':revision,'revision_after':new_revision,'committed_changes':committed,
            'idempotent_replays':replays,'ledger':json.loads(encode(bills))}
    except Exception:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()
