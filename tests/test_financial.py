import pytest
from billing_agent.models import Charge, Evidence
from billing_agent.financial import assess, EntityRegistry
from billing_agent.engine import process


def fee(**kwargs):
    values = dict(charge_item='Handling Charge', amount=40, currency='USD',
                  reference_no='DN1', evidence_ids=['fee'],amount_quote='USD 40')
    values.update(kwargs)
    return Charge(**values)


def role(identity, name, role, source='one.pdf'):
    text=f'{role.upper()}: {name}'
    return Evidence(id=identity,source=source,location='page:1/party:'+identity,text=text,
        structured={'kind':'party_role','role':role,'entity':name})


def primary(text='INVOICE DN1 Handling Charge USD 40'):
    return Evidence(id='fee',source='one.pdf',location='page:1/charge-row:1',text=text)


def test_invoice_explicit_roles_ar_ap_and_third_party():
    ev=[primary(),role('issuer','Alpha LLC','issuer'),role('recipient','Beta LLC','recipient')]
    assert assess(fee(),ev,'Alpha LLC')['posting_type']=='AR'
    assert assess(fee(),ev,'Beta LLC')['posting_type']=='AP'
    assert assess(fee(),ev,'Other LLC')['posting_type'] is None


def test_shipper_consignee_notify_never_substitute_financial_roles():
    ev=[primary(),role('shipper','Alpha LLC','shipper'),role('consignee','Beta LLC','consignee'),
        role('notify','Alpha LLC','notify')]
    out=assess(fee(document_type='Debit Note'),ev,'Beta LLC')
    assert out['posting_type'] is None
    assert out['transaction_party']['issuer'] is None
    assert out['transaction_party']['recipient'] is None


def test_beneficiary_and_letterhead_conflict_blocks():
    ev=[primary(),role('header','Alpha LLC','header_party'),role('beneficiary','Collector LLC','beneficiary')]
    out=assess(fee(),ev,'Alpha LLC')
    assert out['transaction_party']['beneficiary']=='Collector LLC'
    assert out['posting_type'] is None
    assert any('Letterhead' in r for r in out['review_reasons'])


def test_beneficiary_does_not_become_issuer_or_payable_by_itself():
    out=assess(fee(),[primary(),role('bank','Alpha LLC','beneficiary')],'Beta LLC')
    assert out['transaction_party']['issuer'] is None
    assert out['posting_type'] is None


def test_cross_document_roles_cannot_supply_missing_recipient():
    ev=[primary(),role('issuer','Alpha LLC','issuer'),role('recipient','Beta LLC','recipient','other.pdf')]
    assert assess(fee(),ev,'Beta LLC')['posting_type'] is None


def test_explicit_payment_names_kept_literal_and_entity_aliases_are_opt_in():
    ev=[primary('SHA pay TAR: Handling Charge USD 40')]
    assert assess(fee(),ev,'SHA')['posting_type']=='AP'
    assert assess(fee(),ev,'TAR')['posting_type']=='AR'
    assert assess(fee(),ev,'Pilot Aiship Tech co., Ltd.')['posting_type'] is None
    out=assess(fee(),ev,'Pilot Aiship Tech co., Ltd.',{'Pilot Aiship Tech co., Ltd.':['SHA']})
    assert out['posting_type']=='AP'
    assert out['transaction_party']['issuer'] is None


def test_ambiguous_aliases_rejected():
    with pytest.raises(ValueError,match='Ambiguous entity alias'):
        EntityRegistry({'Alpha LLC':['SHA'],'Beta LLC':['sha']})


def test_credit_note_and_disputed_applicability_remain_unresolved():
    ev=[primary(),role('issuer','Alpha LLC','issuer'),role('recipient','Beta LLC','recipient')]
    assert assess(fee(document_type='Credit Note'),ev,'Alpha LLC')['posting_type'] is None
    assert assess(fee(conflict_evidence_ids=['dispute']),ev,'Alpha LLC')['posting_type'] is None


def test_same_canonical_debtor_and_creditor_blocks():
    ev=[primary('A pay B: Handling Charge USD 40')]
    assert assess(fee(),ev,'Alpha',{'Alpha':['A','B']})['posting_type'] is None


def test_arbitrary_direction_quote_does_not_authorize_ar_ap():
    row=fee(issuer='A',recipient='B',direction='ISSUER_CHARGES_RECIPIENT',direction_quote='USD 40')
    assert process(row,[primary() ],[],'B')['financial_attributes']['posting_type'] is None


def test_conflicting_invoice_recipients_block():
    ev=[primary(),role('issuer','Alpha LLC','issuer'),role('r1','Beta LLC','recipient'),role('r2','Gamma LLC','recipient')]
    out=assess(fee(),ev,'Alpha LLC')
    assert out['posting_type'] is None
    assert 'Conflicting recipient identities' in out['review_reasons']


def test_prepaid_collect_or_debit_title_alone_does_not_determine_posting():
    for term in ['PP','CC']:
        out=assess(fee(document_type='Debit Note',payment_term=term),[primary()], 'Alpha LLC')
        assert out['posting_type'] is None


def test_explicit_labels_in_plain_text_support_perspective():
    ev=[primary('INVOICE DN1\nISSUED BY: Alpha LLC\nBILL TO: Beta LLC\nHandling Charge USD 40')]
    assert assess(fee(),ev,'Alpha LLC')['posting_type']=='AR'
    assert assess(fee(),ev,'Beta LLC')['posting_type']=='AP'


def test_roles_on_other_pdf_page_do_not_leak():
    other=role('recipient','Beta LLC','recipient')
    other.location='page:2/party:1'
    ev=[primary(),role('issuer','Alpha LLC','issuer'),other]
    assert assess(fee(),ev,'Beta LLC')['posting_type'] is None


def test_existing_bill_from_other_entity_never_gets_a_patch():
    row={'id':'old-other','document_info':{'reference_no':'DN1'},
        'charge_details':{'charge_item':'Handling Charge','amount':30,'currency':'USD'},
        'financial_attributes':{'accounting_entity':'Other LLC','posting_type':'AP'}}
    charge=fee(intent='MODIFY',explicit_changes=['amount'],change_quote='Change amount to 40')
    ev=[primary('INVOICE DN1\nISSUED BY: Alpha LLC\nBILL TO: Beta LLC\nHandling Charge USD 40\nChange amount to 40')]
    result=process(charge,ev,[row],'Beta LLC')
    assert result['operation']=='PENDING_REVIEW'
    assert result['audit_trail']['proposed_changes']=={}
    assert result['audit_trail']['rejected_entity_scope_bill_ids']==['old-other']


def test_multiple_document_contexts_do_not_establish_a_joint_direction():
    ev=[primary(),role('issuer','Alpha LLC','issuer'),role('recipient','Beta LLC','recipient','other.pdf')]
    row=fee(evidence_ids=['fee','recipient'])
    result=assess(row,ev,'Beta LLC')
    assert result['posting_type'] is None
    assert any('Multiple document' in r for r in result['review_reasons'])
