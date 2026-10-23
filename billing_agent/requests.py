"""Stable logical request identity, independent of filenames and output directories."""
import hashlib
import json
from decimal import Decimal


def request_key(charge,evidence,entity):
    from .engine import normalize
    refs={e.id:e for e in evidence}
    sources=sorted({refs[i].source_digest or hashlib.sha256(refs[i].text.encode()).hexdigest()
                    for i in charge.evidence_ids if i in refs})
    def value(field):
        item=getattr(charge,field)
        return format(Decimal(str(item)).normalize(),'f') if field=='amount' and item is not None else item
    fields={f:value(f) for f in sorted(set(charge.explicit_changes))}
    payload={'entity':entity.strip().casefold(),'sources':sources,'reference':charge.reference_no,
        'charge':normalize(charge.charge_item).casefold(),'intent':charge.intent,
        'fields':fields,'statement':charge.change_quote if charge.intent=='MODIFY' else charge.amount_quote,
        'amount':value('amount'),'currency':charge.currency,'payment_term':charge.payment_term}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
