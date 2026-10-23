import json
from io import BytesIO
from decimal import Decimal

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from billing_agent.parsers import Parser
from billing_agent.remote_ocr import charges
from billing_agent.normalization import normalize_extraction


def pdf(rows, currency='USD', reference='INV-100', pages=1):
    writer=PdfWriter()
    for _ in range(pages):
        page=writer.add_blank_page(600,800)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        text=[]
        def put(x,y,value):
            value=value.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
            text.append(f'BT /F1 10 Tf {x} {y} Td ({value}) Tj ET')
        put(40,750,'INVOICE No: '+reference if reference else 'SHIPPING CHARGES DOCUMENT')
        if currency:put(40,730,'CURRENCY '+currency)
        for x,value in [(40,'CHARGES'),(300,'CUR'),(400,'AMOUNT')]:put(x,700,value)
        for i,(name,amount) in enumerate(rows):
            for x,value in [(40,name),(300,currency),(400,str(amount))]:put(x,675-i*20,value)
        put(40,600,'TOTAL AMOUNT '+str(sum(Decimal(str(r[1])) for r in rows)))
        stream=DecodedStreamObject();stream.set_data('\n'.join(text).encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
    out=BytesIO();writer.write(out);return out.getvalue()


def parse(data, rows, **item):
    import hashlib
    response={'success':True,'data':{'list':[{'chargeList':rows,**item}],'pages_split':[[0,1]]}}
    parser=Parser(pdf_mode='ocr',ocr_cache={hashlib.sha256(data).hexdigest():response})
    parser.parse(data,'bill.pdf')
    return parser,normalize_extraction(charges(parser.evidence),parser.evidence)


def row(name,amount,**extra):return {'chargeItem':name,'amount':amount,**extra}


def test_merged_fees_restore_physical_rows_currency_and_reference():
    p,e=parse(pdf([('Handling Charge',65),('Handling Charge',35),('Documentation Fee',20)]),
        [row('HANDLING CHARGE','100'),row('Documentation Fee','20')],page_index=[0,1])
    assert not any('error' in x for x in p.issues)
    assert [c.amount for c in e.charges]==[Decimal(65),Decimal(35),Decimal(20)]
    assert all(c.currency=='USD' and c.reference_no=='INV-100' for c in e.charges)
    assert len({c.evidence_ids[0] for c in e.charges})==3
    assert any(x['rule']=='merged_rows_restored_from_original' for x in p.remote_adapter_events)


def test_exact_repeated_same_amount_fees_preserved_once_each():
    _,e=parse(pdf([('Handling Charge',40),('Handling Charge',40)]),[row('Handling Charge',40),row('Handling Charge',40)])
    assert len(e.charges)==2


@pytest.mark.parametrize('rows',[ [row('Handling Charge',99)], [row('Handling Charge',100,chargeCtr='ABCD1234567')]])
def test_ambiguous_merge_never_split(rows):
    _,e=parse(pdf([('Handling Charge',65),('Handling Charge',35)]),rows)
    assert len(e.charges)==1 and e.charges[0].warnings
    assert e.charges[0].currency is None and e.charges[0].reference_no is None


def test_missing_currency_and_reference_stay_null():
    _,e=parse(pdf([('Handling Charge',40)],currency='',reference=''),[row('Handling Charge',40)])
    assert e.charges[0].currency is None and e.charges[0].reference_no is None
    assert e.charges[0].warnings


@pytest.mark.parametrize('value',['NaN','Infinity',True,None,'oops'])
def test_invalid_remote_amount_never_accepted(value):
    _,e=parse(pdf([('Handling Charge',40)]),[row('Handling Charge',value)])
    assert e.charges[0].amount is None and e.charges[0].warnings


def test_multi_page_index_not_guessed_and_no_source_borrowing():
    _,e=parse(pdf([('Handling Charge',40)],pages=2),[row('Handling Charge',40)],page_index=[0,1])
    assert e.charges[0].reference_no is None and e.charges[0].currency is None
    assert e.charges[0].warnings


def test_conflicting_remote_currency_and_invoice_reviewed():
    _,e=parse(pdf([('Handling Charge',40)]),[row('Handling Charge',40,currency='EUR')],invoiceNumber='OTHER-999')
    assert any('currency conflicts' in x for x in e.charges[0].warnings)
    assert any('invoice number conflicts' in x for x in e.charges[0].warnings)


def test_missing_remote_fee_does_not_invent_local_candidate():
    p,e=parse(pdf([('Handling Charge',40),('Documentation Fee',20)]),[row('Handling Charge',40)])
    assert len(e.charges)==1
    assert any('omitted' in x for x in e.charges[0].warnings)


def test_adapter_not_sent_to_model_or_duplicated(monkeypatch):
    from billing_agent.extraction import extract
    p,_=parse(pdf([('Handling Charge',40)]),[row('Handling Charge',40)])
    monkeypatch.setenv('BILLING_PROVIDER','ollama')
    monkeypatch.setattr('billing_agent.extraction.extract_local',lambda _:pytest.fail('Unexpected model call'))
    assert len(extract(p.evidence).charges)==1


def test_final_audit_retains_original_remote_response():
    from billing_agent.engine import process_batch
    from billing_agent.financial import enrich_parties
    p,e=parse(pdf([('Handling Charge',40)]),[row('Handling Charge',40)])
    e=enrich_parties(e,p.evidence,'Unknown Entity')
    output=process_batch(e.charges,p.evidence,[],'Unknown Entity')
    assert output[0]['operation']=='PENDING_REVIEW'
    assert output[0]['audit_trail']['remote_ocr_source_evidence'][0]['method']=='ocr_replay'
    assert not any('designated OCR verification still required' in x for x in output[0]['audit_trail']['review_reasons'])


@pytest.mark.parametrize('rows',[None,'not a list',{'amount':40}])
def test_unknown_charge_schema_is_unresolved(rows):
    p,e=parse(pdf([('Handling Charge',40)]),rows)
    assert not e.charges and any(x.get('status')=='unresolved_ocr' for x in p.issues)
