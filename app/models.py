"""Typed boundary between the copilot tools, the policy engine, and Airwallex.

draft_payout produces a PayoutIntent that policy.py prices. Amounts, fees,
limits and the decision to execute stay in code. Sending stays on /api/approve.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Optional[...] rather than `X | None` throughout: Pydantic resolves these
# annotations at runtime, so PEP 604 unions would require Python 3.10+. This
# keeps the app runnable on the macOS system Python as well.

TransferMethod = Literal["LOCAL", "SWIFT"]


class PayoutIntent(BaseModel):
    """What the agent understood from the user's sentence."""

    supplier_query: str = Field(description="Supplier name as the user wrote it")
    amount: Decimal = Field(description="Major units. 100 means one hundred.")
    currency: str = Field(default="", description="ISO-4217, uppercase. Empty if unstated.")
    reference: str = Field(description="Invoice number or payment purpose")
    confidence: float = Field(ge=0.0, le=1.0)
    missing: list[str] = Field(default_factory=list)


class TransferQuery(BaseModel):
    """Which existing transfers to show. Code loads them and applies the filter."""

    scope: Literal["latest", "range"] = "latest"
    limit: int = 1
    since: str = ""
    until: str = ""
    supplier_query: str = ""
    reference: str = ""
    status: str = ""


class CancelQuery(BaseModel):
    """Which existing transfers the user wants stopped. Code picks the ids."""

    supplier_query: str = ""
    amount: Optional[Decimal] = None
    currency: str = ""
    reference: str = ""
    status: str = ""
    scope: Literal["match", "latest", "all"] = "match"
    limit: int = 1


class Supplier(BaseModel):
    """A vetted entry from the supplier directory.

    Bank details come from here, never from the model.
    """

    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    country_code: str
    currency: str
    transfer_method: TransferMethod
    bank_details: dict
    address: dict
    # Set once the supplier has been paid successfully at least once.
    previously_paid: bool = False
    # Populated after the Airwallex beneficiary is created.
    beneficiary_id: Optional[str] = None


class Decision(str, Enum):
    AUTO_EXECUTE = "auto_execute"
    REQUIRES_APPROVAL = "requires_approval"


class PayoutPlan(BaseModel):
    """A fully priced, validated payout awaiting a decision.

    `request_id` is minted here and reused on every retry of this same plan, so
    a retry can never create a second transfer. `fingerprint` hashes the
    money-moving fields; an approval is only valid for the exact plan the human
    saw on screen.
    """

    plan_id: str
    request_id: str
    fingerprint: str

    supplier: Supplier
    amount: Decimal
    currency: str
    reference: str

    fee: Decimal
    fee_currency: str
    total_debit: Decimal
    available_balance: Decimal

    decision: Decision
    reasons: list[str]


class TransferResult(BaseModel):
    transfer_id: Optional[str]
    status: str
    failure_type: Optional[str] = None
    failure_reason: Optional[str] = None
    raw: dict = Field(default_factory=dict)
