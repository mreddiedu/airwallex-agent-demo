"""Anthropic tools the copilot may call.

None of these send money. draft_payout prices a plan through policy.build_plan
and returns a receipt. Sending stays on /api/approve after a PIN.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .httplog import using_tool
from .models import CancelQuery, PayoutIntent, TransferQuery

log = logging.getLogger("payout.tools")
# The id goes into a URL path. Anything else could walk to another endpoint.
_TRANSFER_ID = re.compile(r"[A-Za-z0-9-]{1,64}")

TOOL_NAMES = (
    "get_balances",
    "list_transfers",
    "list_suppliers",
    "get_transfer",
    "draft_payout",
    "draft_cancel",
)

TOOLS = [
    {
        "name": "get_balances",
        "description": "Read the live wallet balances. Does not move money.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_transfers",
        "description": (
            "List existing transfers. Use this to show history or a specific payout. "
            "Does not cancel or send anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["latest", "range"],
                    "description": "latest is the most recent by time. range needs since/until.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How many, for latest only. Last transfer = 1. Leave empty for a date or other range: every match is returned.",
                },
                "since": {"type": "string", "description": "Local ISO-8601 start, or empty."},
                "until": {"type": "string", "description": "Local ISO-8601 end, or empty."},
                "supplier_query": {"type": "string", "description": "Payee name, or empty."},
                "reference": {"type": "string", "description": "Invoice or transfer id, or empty."},
                "status": {
                    "type": "string",
                    "description": "PROCESSING, SCHEDULED, SENT, PAID, CANCELLED, or empty.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_transfer",
        "description": "Read one existing transfer by id. Does not change it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "transfer_id": {"type": "string", "description": "Airwallex transfer id."},
            },
            "required": ["transfer_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_suppliers",
        "description": "The vetted supplier directory. Look a payee up before drafting a payout.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "draft_payout",
        "description": (
            "Price a payout and return the receipt to show the operator. "
            "Does not send money. A human must approve with a PIN before anything is created."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_query": {
                    "type": "string",
                    "description": "Payee as the user referred to them.",
                },
                "amount": {
                    "type": "string",
                    "description": "Major units, no symbols. 4000 means four thousand.",
                },
                "currency": {
                    "type": "string",
                    "description": "ISO-4217 uppercase, or empty if never stated.",
                },
                "reference": {
                    "type": "string",
                    "description": "Invoice or purpose. Required before a receipt can be held.",
                },
            },
            "required": ["supplier_query", "amount"],
            "additionalProperties": False,
        },
    },
    {
        "name": "draft_cancel",
        "description": (
            "Find the transfers a cancel would stop and return them. "
            "Does not cancel. A human must say yes before anything is cancelled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_query": {"type": "string"},
                "amount": {"type": "string", "description": "Major units, or empty."},
                "currency": {"type": "string"},
                "reference": {"type": "string", "description": "Invoice, short id, or transfer id."},
                "status": {"type": "string"},
                "scope": {"type": "string", "enum": ["match", "latest", "all"]},
                "limit": {"type": "integer", "description": "How many, for latest."},
            },
            "additionalProperties": False,
        },
    },
]


class ToolHost(Protocol):
    """Airwallex reads plus draft_payout. Must not expose create_transfer."""

    async def get_balances(self) -> dict: ...

    async def list_transfers(self, query: TransferQuery) -> dict: ...

    async def list_suppliers(self) -> dict: ...

    async def get_transfer(self, transfer_id: str) -> dict: ...

    async def draft_payout(self, intent: PayoutIntent) -> dict: ...

    async def draft_cancel(self, query: CancelQuery) -> dict: ...


def dump_tool_result(payload: dict) -> str:
    return json.dumps(payload, default=str)


async def call_tool(name: str, arguments: dict | None, host: ToolHost) -> dict:
    """Run one allowed tool. Unknown names are refused and never forwarded."""
    if name not in TOOL_NAMES:
        return {
            "ok": False,
            "error": f"{name} is not available. Nothing was sent.",
        }
    args = arguments if isinstance(arguments, dict) else {}
    with using_tool(name):
        try:
            return await _dispatch(name, args, host)
        except Exception as exc:
            log.exception("tool %s failed", name)
            return {"ok": False, "error": f"{name} failed: {exc}. Nothing was sent.", "sent": False}


async def _dispatch(name: str, args: dict, host: ToolHost) -> dict:
    if name == "get_balances":
        return await host.get_balances()
    if name == "list_suppliers":
        return await host.list_suppliers()
    if name == "list_transfers":
        return await host.list_transfers(_transfer_query(args))
    if name == "get_transfer":
        transfer_id = str(args.get("transfer_id") or "").strip()
        if not _TRANSFER_ID.fullmatch(transfer_id):
            return {"ok": False, "error": "That isn't a transfer id.", "sent": False}
        return await host.get_transfer(transfer_id)
    if name == "draft_cancel":
        query = _cancel_query(args)
        if isinstance(query, dict):
            return query
        return await host.draft_cancel(query)
    intent = _payout_intent(args)
    if isinstance(intent, dict):
        return intent
    return await host.draft_payout(intent)


def _transfer_query(args: dict) -> TransferQuery:
    scope = str(args.get("scope") or "latest").lower()
    if scope not in ("latest", "range"):
        scope = "latest"
    since = str(args.get("since") or "").strip()
    until = str(args.get("until") or "").strip()
    # A date window returns every match. A "last N" request keeps its own count.
    if scope == "range" or since or until:
        limit = 0
    else:
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, limit)
    return TransferQuery(
        scope=scope,  # type: ignore[arg-type]
        limit=limit,
        since=since,
        until=until,
        supplier_query=str(args.get("supplier_query") or "").strip(),
        reference=str(args.get("reference") or "").strip(),
        status=str(args.get("status") or "").upper().strip(),
    )


def _cancel_query(args: dict) -> CancelQuery | dict[str, Any]:
    amount = None
    text = str(args.get("amount") or "").replace(",", "").strip()
    if text:
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return {"ok": False, "error": "Which amount should I cancel?", "sent": False}
        if not amount.is_finite() or amount <= 0:
            amount = None
    scope = str(args.get("scope") or "match").lower()
    if scope not in ("match", "latest", "all"):
        scope = "match"
    try:
        limit = int(args.get("limit") or 1)
    except (TypeError, ValueError):
        limit = 1
    return CancelQuery(
        supplier_query=str(args.get("supplier_query") or "").strip(),
        amount=amount,
        currency=str(args.get("currency") or "").upper().strip(),
        reference=str(args.get("reference") or "").strip(),
        status=str(args.get("status") or "").upper().strip(),
        scope=scope,  # type: ignore[arg-type]
        limit=min(6, max(1, limit)),
    )


def _payout_intent(args: dict) -> PayoutIntent | dict[str, Any]:
    supplier_query = str(args.get("supplier_query") or "").strip()
    if not supplier_query:
        return {"ok": False, "error": "Who should I pay?"}
    try:
        amount = Decimal(str(args.get("amount") or "").replace(",", "").replace(" ", "").strip())
    except (InvalidOperation, AttributeError):
        return {"ok": False, "error": "How much should I pay?"}
    if not amount.is_finite() or amount <= 0:
        return {"ok": False, "error": "I need an amount greater than zero."}
    currency = str(args.get("currency") or "").upper().strip()
    if currency and len(currency) != 3:
        currency = ""
    missing = []
    if not currency:
        missing.append("currency")
    reference = str(args.get("reference") or "").strip()
    return PayoutIntent(
        supplier_query=supplier_query,
        amount=amount,
        currency=currency,
        reference=reference,
        confidence=1.0,
        missing=missing,
    )
