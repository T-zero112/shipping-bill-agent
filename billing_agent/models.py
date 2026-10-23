from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field, field_validator
import re


def parse_amount(value):
    if isinstance(value, str) and re.fullmatch(r'-?\d{1,3}(,\d{3})+(\.\d+)?', value):
        return value.replace(',', '')
    return value


class Evidence(BaseModel):
    id: str
    source: str
    location: str
    text: str
    method: str = 'parser'
    parent_id: str | None = None
    bbox: list[float] | None = None
    structured: dict | None = None
    source_digest: str | None = None


class Charge(BaseModel):
    _parse_amount = field_validator('amount', mode='before')(parse_amount)
    _parse_business_numbers = field_validator('quantity', 'unit_price', 'subtotal', 'tax', 'document_total', mode='before')(parse_amount)
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    charge_item: str
    charge_code: str | None = None
    source_description: str | None = None
    name_quote: str | None = None
    conflict_evidence_ids: list[str] = Field(default_factory=list)
    party_evidence_ids: list[str] = Field(default_factory=list)
    amount: Decimal | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    document_total: Decimal | None = None
    business_evidence: dict[str, dict[str, str]] = Field(default_factory=dict)
    transport_identifiers: list[dict[str,str]] = Field(default_factory=list)
    currency: str | None = None
    payment_term: str | None = None
    bill_of_lading: str | None = None
    booking_no: str | None = None
    container_no: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    amount_quote: str | None = None
    actual_charge: bool = True
    intent: Literal['OBSERVE', 'MODIFY'] = 'OBSERVE'
    explicit_changes: list[Literal['amount', 'currency', 'payment_term']] = Field(default_factory=list)
    change_quote: str | None = None
    field_evidence: dict[str,str] = Field(default_factory=dict)
    target_quote: str | None = None
    # A role is useful only when supported by an explicit direction quote.
    direction: Literal['ISSUER_CHARGES_RECIPIENT', 'UNKNOWN'] = 'UNKNOWN'
    direction_quote: str | None = None
    warnings: list[str] = Field(default_factory=list)


class Extraction(BaseModel):
    recovery_trace: list[dict] = Field(default_factory=list)
    charges: list[Charge] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    totals: list['DocumentTotal'] = Field(default_factory=list)
    excluded_candidates: list[dict] = Field(default_factory=list)
    normalization_events: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    model_responses: list[dict] = Field(default_factory=list)


class DocumentTotal(BaseModel):
    _parse_amount = field_validator('amount', mode='before')(parse_amount)
    reference_no: str
    currency: str
    amount: Decimal
    evidence_id: str
    quote: str


Extraction.model_rebuild()
