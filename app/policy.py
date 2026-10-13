"""Deterministic policy engine.

Everything that decides whether money moves lives here, in code, so the same
input always produces the same decision and the rules can be read in review.
The model's confidence score is an input to this, never a substitute for it.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from decimal import Decimal

from .airwallex import fee_for
from .models import Decision, PayoutIntent, PayoutPlan, Supplier
from .suppliers import all_suppliers, resolve

# Below this, a payout to an already-paid supplier executes without a human.
# Default 0 means every payout is held for approval.
def auto_approve_limit() -> Decimal:
    return Decimal(os.getenv("AUTO_APPROVE_LIMIT_USD", "0"))


# The model must be at least this sure before we will even offer to execute.
MIN_CONFIDENCE = 0.80


class PolicyRejection(Exception):
    """Raised when the request cannot become a plan at all."""

    def __init__(self, message: str, question: str | None = None):
        super().__init__(message)
        self.question = question


def build_plan(
    intent: PayoutIntent,
    balances: dict[str, Decimal],
) -> PayoutPlan:
    """Price and classify a payout. Does not execute anything."""

    supplier, candidates = resolve(intent.supplier_query)
    if supplier is None:
        directory = "; ".join(f"{s.name} ({s.currency})" for s in all_suppliers())
        if not candidates:
            raise PolicyRejection(
                f"I don't have '{intent.supplier_query}' on file.",
                question=(
                    f"I don't know '{intent.supplier_query}', and I won't guess their bank details. "
                    f"I can pay: {directory}."
                ),
            )
        names = ", ".join(s.name for s in candidates)
        raise PolicyRejection(
            f"A few suppliers match that: {names}",
            question=f"Did you mean {names}?",
        )

    reasons: list[str] = []
    decision = Decision.REQUIRES_APPROVAL

    # Isolated copy so later directory updates cannot change a shown plan.
    supplier = supplier.model_copy(deep=True)

    # --- hard blocks: these never reach a human as an approvable action ---

    if not intent.amount.is_finite() or intent.amount <= 0:
        raise PolicyRejection("I need an amount greater than zero.")

    currency = (intent.currency or "").upper()
    if not currency:
        currency = supplier.currency
        reasons.append(
            f"You didn't name a currency, so I'll use {currency}, which is what {supplier.name} receives."
        )
        if "currency" not in intent.missing:
            intent.missing = list(intent.missing) + ["currency"]

    if currency != supplier.currency:
        alt = [
            s.name for s in all_suppliers() if s.currency == currency and s.id != supplier.id
        ]
        alt_txt = f" {alt[0]} receives {currency}." if alt else ""
        raise PolicyRejection(
            f"{supplier.name} only receives {supplier.currency}, "
            f"and this request is in {currency}.",
            question=(
                f"{supplier.name} can only take {supplier.currency}.{alt_txt} "
                f"Want me to pay in {supplier.currency}, or should we pick someone else?"
            ),
        )

    fee, fee_currency = fee_for(supplier.transfer_method)
    # Fee is charged in EUR from the same wallet; for the demo we only compare
    # against balance when the fee shares the payout currency.
    total_debit = intent.amount + (fee if fee_currency == currency else Decimal("0"))
    available = balances.get(currency, Decimal("0"))

    if total_debit > available:
        raise PolicyRejection(
            f"There isn't enough {currency} for this. It needs {total_debit}, and the wallet has {available}."
        )

    # --- graduated controls ------------------------------------------------

    if intent.confidence < MIN_CONFIDENCE:
        reasons.append(
            f"Low confidence ({intent.confidence:.0%}); I'll wait for you to confirm the details."
        )
    if intent.missing:
        reasons.append(f"Not stated in the request: {', '.join(intent.missing)}.")
    if not supplier.previously_paid:
        reasons.append(f"First payment to {supplier.name}, so I'll always check with you.")
    intent.reference = (intent.reference or "").strip()
    if not intent.reference:
        raise PolicyRejection(
            "I need an invoice or reference before this can go out.",
            question="What invoice or reference should I put on this?",
        )
    if supplier.transfer_method == "SWIFT":
        reasons.append(f"SWIFT adds a flat {fee} {fee_currency} fee.")

    limit = auto_approve_limit()
    blocking = [
        intent.confidence < MIN_CONFIDENCE,
        bool(intent.missing),
        not supplier.previously_paid,
        intent.amount > limit,
    ]
    if not any(blocking):
        decision = Decision.AUTO_EXECUTE
        reasons.append(
            f"Within the {limit} auto-approval limit for an established supplier."
        )
    else:
        if intent.amount > limit:
            reasons.append(
                f"{intent.amount} {currency} exceeds the {limit} auto-approval limit."
            )

    request_id = str(uuid.uuid4())
    plan = PayoutPlan(
        plan_id=str(uuid.uuid4()),
        request_id=request_id,
        fingerprint="",
        supplier=supplier,
        amount=intent.amount,
        currency=currency,
        reference=intent.reference or f"Payout {request_id[:8]}",
        fee=fee,
        fee_currency=fee_currency or currency,
        total_debit=total_debit,
        available_balance=available,
        decision=decision,
        reasons=reasons,
    )
    plan.fingerprint = fingerprint(plan)
    return plan


def fingerprint(plan: PayoutPlan) -> str:
    """Hash of exactly the fields a human approves.

    The approval is checked against this before execution, so a plan whose
    amount, payee, rail or fee changed after approval cannot be executed on the
    old consent.
    """
    material = "|".join(
        [
            plan.supplier.id,
            str(plan.amount),
            plan.currency,
            str(plan.fee),
            plan.fee_currency,
            plan.supplier.transfer_method,
            plan.reference,
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]
