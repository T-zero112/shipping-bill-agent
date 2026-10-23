"""Validate final records without stripping inherited old-ledger fields."""
import json
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from .models import Evidence


class Object(BaseModel):
    model_config=ConfigDict(extra='allow',allow_inf_nan=False)


class Document(Object):
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None

    @field_validator('document_date')
    @classmethod
    def iso_date(cls,value):
        if value is not None:
            from datetime import date
            if date.fromisoformat(value).isoformat()!=value:
                raise ValueError('Output date must be YYYY-MM-DD or null')
        return value


class Parties(Object):
    issuer: str | None = None
    recipient: str | None = None
    payer: str | None = None
    payee: str | None = None
    beneficiary: str | None = None


class Details(Object):
    charge_item: str = Field(min_length=1)
    amount: Decimal | None
    currency: str | None
    payment_term: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    charge_code: str | None = None

    @field_validator('amount','quantity','unit_price',mode='before',json_schema_input_type=float | None)
    @classmethod
    def numeric(cls,value):
        if value is not None and (isinstance(value,(bool,str)) or not isinstance(value,(int,float,Decimal))):
            raise ValueError('Output amounts must be JSON numbers or null, not strings/bools')
        return value


class Financial(Object):
    accounting_entity: str = Field(min_length=1)
    posting_type: Literal['AR','AP'] | None


class Audit(Object):
    ai_reasoning: list[str] = Field(min_length=1)
    evidence: list[Evidence]
    review_reasons: list[str]


class Transport(Object):
    bill_of_lading: str | None = None
    booking_no: str | None = None
    container_no: str | None = None
    master_bills: list[str] = Field(default_factory=list)
    house_bills: list[str] = Field(default_factory=list)
    containers: list[str] = Field(default_factory=list)
    bills: list[str] = Field(default_factory=list)


class Amounts(Object):
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    document_total: Decimal | None = None
    _numeric=field_validator('subtotal','tax','document_total',mode='before',json_schema_input_type=float | None)(Details.numeric.__func__)


class CreditAdjustment(Object):
    original_reference_no: str | None
    original_bill_id: str | None
    credit_amount: Decimal | None
    signed_amount: Decimal | None
    remaining_before: Decimal | None
    remaining_after: Decimal | None
    original_amount: Decimal | None = None
    credited_before: Decimal | None = None
    balance_effect: Literal['REDUCE']
    posting_type: Literal['AR','AP'] | None
    proofs: list[dict[str,str]]
    _numeric=field_validator('credit_amount','signed_amount','remaining_before','remaining_after','original_amount','credited_before',mode='before',json_schema_input_type=float | None)(Details.numeric.__func__)


class Record(Object):
    operation: Literal['CREATE','UPDATE','NO_CHANGE','PENDING_REVIEW']
    document_info: Document
    transaction_party: Parties
    charge_details: Details
    financial_attributes: Financial
    audit_trail: Audit
    transport_info: Transport | None = None
    document_amounts: Amounts | None = None
    credit_adjustment: CreditAdjustment | None = None

    @model_validator(mode='after')
    def posting_invariants(self):
        if self.operation in {'CREATE','UPDATE'}:
            import pycountry
            code=self.charge_details.currency
            if self.charge_details.amount is None or not code or not pycountry.currencies.get(alpha_3=code):
                raise ValueError('CREATE/UPDATE requires amount and valid ISO currency')
            if self.financial_attributes.posting_type is None or not self.audit_trail.evidence or self.audit_trail.review_reasons:
                raise ValueError('CREATE/UPDATE requires resolved posting, evidence and no review reasons')
        if self.operation=='PENDING_REVIEW' and not self.audit_trail.review_reasons:
            raise ValueError('PENDING_REVIEW requires review reasons')
        if self.operation in {'CREATE','UPDATE'} and self.credit_adjustment:
            c=self.credit_adjustment
            if (self.operation!='CREATE' or not c.original_bill_id or not c.original_reference_no or not c.proofs
                    or c.credit_amount is None or c.credit_amount<=0 or c.signed_amount!=-c.credit_amount
                    or self.charge_details.amount!=c.signed_amount or c.posting_type!=self.financial_attributes.posting_type
                    or c.remaining_before is None or c.remaining_after!=c.remaining_before-c.credit_amount or c.remaining_after<0
                    or c.original_amount is None or c.credited_before is None or c.credited_before<0
                    or c.remaining_before!=c.original_amount-c.credited_before):
                raise ValueError('Invalid credit original association, signed amount or remaining balance')
        if self.operation in {'CREATE','UPDATE'} and 'credit' in (self.document_info.document_type or '').lower() and not self.credit_adjustment:
            raise ValueError('Credit note requires validated separate credit adjustment')
        return self


ADAPTER=TypeAdapter(list[Record])


class OutputContractError(ValueError):
    pass


def validate_results(results):
    try:
        ADAPTER.validate_python(results)
        # Verify the exact serializer used by the CLI, including inherited extras.
        payload=json.dumps(results,ensure_ascii=False,allow_nan=False,
                           default=lambda value:float(value) if isinstance(value,Decimal) else str(value))
        ADAPTER.validate_python(json.loads(payload))
    except (ValueError,TypeError) as exc:
        raise OutputContractError(str(exc)) from exc
    return results
