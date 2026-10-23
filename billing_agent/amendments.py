"""Conservative compiler for explicit EN/ZH bill amendments, with field-level proof.

Supported grammar is intentionally bounded. Unsupported/conditional amendments are
review candidates, never silently converted into new bills or executable patches.
"""
import re
from decimal import Decimal
from .models import Charge
from .names import FEE_PHRASE

ACTION = re.compile(r'\b(?:change|update|set|amend|revise)\b|(?:修改|改为|改成|调整为|更新|设为)',re.I)
UNSAFE = re.compile(r'\b(?:if|unless|would|could|cancel|cancelled|ignore previous|previous request|earlier request|for reference|no changes?|do not|don.t|pending approval)\b|如果|若|假如|不要|无需|取消|撤销|是否|能否|请勿|确认后|审批后|历史指令|原邮件|此前',re.I)
NUMBER = r'-?\d+(?:,\d{3})*(?:\.\d+)?'
TO = r'(?:\bto\b|为|至|成)'
UNSUPPORTED = re.compile(r'\b(?:delete|remove|clear|erase|null|issuer|recipient|accounting entity|bank(?: account)?|due date|document date|quantity)\b|清空|删除|签发方|接收方|记账主体|银行|账户|数量|日期',re.I)


def field_values(text):
    """Only field-specific assertions authorize a patch; code beside amount is context."""
    values={}
    amount=re.search(r'(?:\bamount\b|金额)\s*(?:(?:change|update|改|调整|修改|设)\s*)?'+TO+r'\s*(?P<currency>[A-Z]{3})?\s*(?P<amount>'+NUMBER+r')',text,re.I)
    if not amount:
        amount=re.search(r'(?:改为|调整为|设为|\bto\b)\s*(?P<currency>[A-Z]{3})\s*(?P<amount>'+NUMBER+r')',text,re.I)
    if amount:
        values['amount']={'value':Decimal(amount.group('amount').replace(',','')),'quote':amount.group(0),
                          'currency_context':amount.group('currency').upper() if amount.group('currency') else None}
    currency=re.search(r'(?:\bcurrency\b|币种)\s*(?:(?:change|update|改|调整|修改|设)\s*)?'+TO+r'\s*([A-Z]{3})\b',text,re.I)
    if currency:
        values['currency']={'value':currency.group(1).upper(),'quote':currency.group(0)}
    term=re.search(r'(?:\bpayment(?:\s+term)?\b|付款方式|支付方式)\s*(?:(?:change|update|改|调整|修改|设)\s*)?'+TO+r'\s*(PP|CC)\b',text,re.I)
    if term:
        values['payment_term']={'value':term.group(1).upper(),'quote':term.group(0)}
    return values


def parse_amendment(evidence):
    text=evidence.text.strip()
    if not ACTION.search(text):
        return None
    if text.startswith('>') or (evidence.structured or {}).get('quoted'):
        return None
    targets=list(re.finditer(r'(?<![\w-])(?:DN|INV|CN)[-][A-Za-z0-9]+(?:[-][A-Za-z0-9]+)*(?![\w-])',text,re.I))
    fees=list(FEE_PHRASE.finditer(text))
    warnings=[]
    if len(targets)!=1:
        warnings.append('Amendment must identify exactly one bill reference in the same statement')
    if len(fees)!=1:
        warnings.append('Amendment must identify exactly one supported charge name')
    if UNSAFE.search(text):
        warnings.append('Conditional, negative, cancelled or interrogative amendment requires review')
    values=field_values(text)
    if not values:
        warnings.append('No supported explicit field change; clearing fields is not allowed')
    if UNSUPPORTED.search(text):
        warnings.append('Instruction includes an unsupported field or destructive change')
    # Repeated field changes or unparsed requests are not silently discarded.
    for label in ['amount','currency','payment term','金额','币种','付款方式']:
        if len(re.findall(re.escape(label),text,re.I))>1:
            warnings.append('Repeated field instructions require review')
    return Charge(reference_no=targets[0].group(0) if len(targets)==1 else None,
        target_quote=targets[0].group(0) if len(targets)==1 else None,
        charge_item=fees[0].group(0) if len(fees)==1 else 'Unresolved amendment target',
        source_description=fees[0].group(0) if len(fees)==1 else None,
        name_quote=fees[0].group(0) if len(fees)==1 else None,
        amount=values.get('amount',{}).get('value'),
        currency=values.get('currency',{}).get('value') or values.get('amount',{}).get('currency_context'),
        payment_term=values.get('payment_term',{}).get('value'),
        amount_quote=values.get('amount',{}).get('quote'),evidence_ids=[evidence.id],
        intent='MODIFY',explicit_changes=list(values),change_quote=text,
        field_evidence={name:item['quote'] for name,item in values.items()},warnings=warnings)


def patch_proof(charge, evidence):
    refs={e.id:e for e in evidence}
    primary=[refs[i] for i in charge.evidence_ids if i in refs]
    quote=charge.change_quote
    errors=[]
    if not quote or not any(quote in e.text for e in primary):
        return ['Modification lacks explicit source instruction']
    if not ACTION.search(quote) or UNSAFE.search(quote) or any((e.structured or {}).get('quoted') for e in primary):
        errors.append('Modification instruction is absent, quoted, conditional or negated')
    if UNSUPPORTED.search(quote):
        errors.append('Modification contains unsupported or destructive field instructions')
    proven=field_values(quote)
    if not charge.explicit_changes:
        errors.append('No explicitly authorized fields')
    for field in charge.explicit_changes:
        if field not in proven or getattr(charge,field) != proven[field]['value']:
            errors.append(f'Field {field} is not authorized by its source instruction')
        field_quote=charge.field_evidence.get(field)
        if field_quote and (field_quote not in quote or field not in proven or field_quote != proven[field]['quote']):
            errors.append(f'Invalid field-level evidence for {field}')
    if charge.target_quote and (charge.target_quote not in quote or charge.reference_no != charge.target_quote):
        errors.append('Amendment target is not supported by the instruction')
    if charge.reference_no and not any(re.search(r'(?<![\w-])'+re.escape(charge.reference_no)+r'(?![\w-])',e.text) for e in primary):
        errors.append('Bill reference is not present in the primary instruction evidence')
    from .engine import normalize
    names=list(FEE_PHRASE.finditer(quote))
    if not names:
        names=[m for e in primary for m in FEE_PHRASE.finditer(e.text)]
    if not any(normalize(m.group(0)).casefold()==normalize(charge.charge_item).casefold() for m in names):
        errors.append('Charge target name is not supported by the modification instruction')
    return errors
