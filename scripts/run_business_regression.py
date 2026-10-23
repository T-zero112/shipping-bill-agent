"""Reproducible synthetic evidence -> fields -> four-tier old-ledger matching."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billing_agent.models import Charge, Evidence, Extraction
from billing_agent.business import enrich_business
from billing_agent.engine import process
from billing_agent.__main__ import dump


def main():
    root = Path('outputs/business-fields/matching')
    root.mkdir(parents=True, exist_ok=True)
    checks = []
    for tier in range(1,5):
        lines = ['A charges B. Handling Fee USD 40', 'Invoice Date: 2026-10-21',
                 'Quantity: 2 Unit Price: 20', 'Payment Terms: PP',
                 'Subtotal: USD 40', 'Tax: USD 4', 'Total Amount: USD 44']
        if tier <= 2:
            lines.append('B/L No: BL12345')
        if tier <= 3:
            lines.append('Booking No: BK12345')
        lines.append('Container No: ABCD1234567')
        ev = [Evidence(id='e',source='synthetic.txt',location='row',text='\n'.join(lines))]
        c = Charge(reference_no='INV123' if tier==1 else None, charge_item='Handling Fee',
                   amount=40,currency='USD',issuer='A',recipient='B', evidence_ids=['e'],
                   amount_quote='USD 40',direction='ISSUER_CHARGES_RECIPIENT',direction_quote='A charges B.')
        extraction = enrich_business(Extraction(charges=[c]),ev)
        c = extraction.charges[0]
        old = {'id':'old-1', 'document_info':{'reference_no':'INV123','document_date':'2026-10-21'},
               'transport_info':{'bill_of_lading':'BL12345','booking_no':'BK12345','container_no':'ABCD1234567'},
               'transaction_party':{'issuer':'A','recipient':'B'},
               'charge_details':{'charge_item':'Handling Charge','amount':40,'currency':'USD',
                                 'payment_term':'PP','quantity':2,'unit_price':20},
               'financial_attributes':{'accounting_entity':'B','posting_type':'AP'},'protected_note':'preserve'}
        result = process(c,ev,[old],'B')
        folder = root/f'tier-{tier}'
        folder.mkdir(exist_ok=True)
        for name, value in [('existing.json',[old]),('evidence.json',[e.model_dump() for e in ev]),
                            ('extraction.json',extraction.model_dump()),('result.json',[result])]:
            dump(folder/name,value)
        checks.append({'tier':tier,'operation':result['operation'],
                       'passed':result['operation']=='NO_CHANGE' and result['audit_trail']['matched_bill_ids']==['old-1']
                                and result['protected_note']=='preserve'})
    report={'dataset':'Synthetic old bills and source text; not company history or LLM verification',
            'checks':checks,'passed':all(c['passed'] for c in checks)}
    dump(root/'validation.json',report)
    print(json.dumps(report,indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
