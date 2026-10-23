"""Generate explicitly synthetic data for reproducible CREATE/UPDATE/NO_CHANGE/review demos."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billing_agent.parsers import Parser

root = Path('outputs/synthetic-demo')
root.mkdir(parents=True, exist_ok=True)
text = 'A charges B. DN1 Handling Fee USD 40. Change amount to 40 only.'
(root/'input.txt').write_text(text, encoding='utf-8')
parser = Parser(); parser.parse(text.encode(), 'input.txt')
fixture = {'charges':[{'reference_no':'DN1', 'issuer':'A', 'recipient':'B',
    'charge_item':'Handling Fee', 'amount':40, 'currency':'USD',
    'evidence_ids':[parser.evidence[0].id], 'amount_quote':'USD 40',
    'direction':'ISSUER_CHARGES_RECIPIENT','direction_quote':'A charges B.'}]}
old = {'id':'synthetic-bill-1', 'document_info':{'reference_no':'DN1','document_date':'2026-01-01'},
    'transaction_party':{'issuer':'A','recipient':'B'},
    'charge_details':{'charge_item':'Handling Charge','amount':30,'currency':'USD','payment_term':'PP'},
    'financial_attributes':{'accounting_entity':'B','posting_type':'AP'}}
for name, data in [('create.json',fixture), ('existing.json',[old])]:
    (root/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
fixture['charges'][0].update(intent='MODIFY',explicit_changes=['amount'],change_quote='Change amount to 40 only.')
(root/'update.json').write_text(json.dumps(fixture,indent=2),encoding='utf-8')
print('Synthetic fixtures generated under outputs/synthetic-demo; not company samples.')
