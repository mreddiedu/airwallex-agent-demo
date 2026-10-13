"""Payout Copilot — FastAPI app.

/api/chat runs Claude with read and draft tools. Drafts are priced by policy
and held. Money moves only in /api/approve (yes, PIN, matching fingerprint)
and /api/cancel-transfers (yes on the exact set shown).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import MAX_TOOL_ROUNDS, Copilot
from .airwallex import AirwallexClient, AirwallexError, _transfer_result
from .cancel import (
    fingerprint as cancel_fingerprint,
    looks_like_cancel,
    newest_first,
    parse_created_at,
    propose,
    reference_matches,
    resolve_queries,
    view_transfer,
)
from .invoice_reader import MAX_FILES, payout_message, read_invoice
from .invoices import already_sent, next_invoice
from .httplog import clear as clear_http_trace
from .httplog import latest as http_trace_latest
from .httplog import since as http_trace_since
from .models import PayoutIntent, PayoutPlan, TransferQuery
from .policy import PolicyRejection, build_plan, fingerprint
from .sandbox_progress import countdown, start as start_sandbox_progress, step_for
from .suppliers import all_suppliers, by_id, mark_paid, resolve

load_dotenv()
# A reload inherits the process environment, which still has the previous model.
# The file is the source of truth, so apply it again here.
_model = dotenv_values().get("ANTHROPIC_MODEL")
if _model:
    os.environ["ANTHROPIC_MODEL"] = _model

log = logging.getLogger("payout")
_LOG_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"


def _stamp_logs() -> None:
    """Put a timestamp on every log line, including uvicorn's own handlers."""
    seen: set[int] = set()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "payout", "payout.http"):
        for handler in logging.getLogger(name).handlers:
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            current = handler.formatter
            pattern = getattr(current, "_fmt", "") or ""
            if pattern.startswith("%(asctime)s"):
                if current is not None:
                    current.datefmt = _LOG_DATE
                continue
            updated = "%(asctime)s " + (pattern or "%(levelname)s %(name)s: %(message)s")
            kwargs: dict = {"fmt": updated, "datefmt": _LOG_DATE}
            if current is not None and "use_colors" in type(current).__init__.__code__.co_varnames:
                kwargs["use_colors"] = getattr(current, "use_colors", None)
            try:
                handler.setFormatter(type(current)(**kwargs) if current is not None else logging.Formatter(updated, _LOG_DATE))
            except TypeError:
                handler.setFormatter(logging.Formatter(updated, _LOG_DATE))
            if name == "uvicorn.access":
                handler.setStream(sys.stderr)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _QuietPolls) for f in access.filters):
        access.addFilter(_QuietPolls())


class _QuietPolls(logging.Filter):
    """The HTTP console polls /api/http every second. Those lines bury the rest."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/http?" not in record.getMessage()


_stamp_logs()

STATIC_DIR = Path(__file__).parent / "static"
STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"

# Pending plans awaiting approval, and an audit trail of everything decided.
PLANS: dict[str, PayoutPlan] = {}
PLAN_ORDER: list[str] = []
CANCEL_BATCH: dict | None = None
AUDIT: list[dict] = []
SESSIONS: dict[str, list[dict]] = defaultdict(list)
# Plans currently inside /approve. A second click must not start a second send.
IN_FLIGHT: set[str] = set()
# Wrong PINs across every plan. A new draft must not reset the count.
PIN_MISSES = 0
PIN_LIMIT = 3
ADMIN_PHONE = "1-800-GET-HELP"
ACCOUNT_LOCKED = False


def _lock_message() -> str:
    return (
        "Incorrect pin. I'm sorry — your account is locked. "
        f"Please call System Admin at {ADMIN_PHONE} to unlock."
    )


def _require_unlocked() -> None:
    if ACCOUNT_LOCKED:
        raise HTTPException(status_code=403, detail=_lock_message())


def _run_policy_tests() -> None:
    """Offline guardrails. A failure stops startup."""
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "test_core.py"
    log.info("running %s", script.name)
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("%s failed\n%s", script.name, (result.stdout + result.stderr).strip())
        raise RuntimeError(f"{script.name} failed. Server not started.")
    log.info("core tests passed")


def _run_preflight() -> None:
    """Live, read-only checks in the background. A failure is logged loudly
    but does not stop the server, so a sandbox blip can't block a restart."""
    if os.getenv("PREFLIGHT", "1") == "0":
        return
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "preflight.py"

    def run() -> None:
        try:
            result = subprocess.run(
                [sys.executable, str(script)], cwd=str(root), capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            log.error("preflight timed out after 120s. Airwallex or Claude may be unreachable.")
            return
        lines = (result.stdout + result.stderr).strip().splitlines()
        if result.returncode == 0:
            warnings = [line.strip() for line in lines if line.strip().startswith("WARN")]
            log.info("preflight passed (live sandbox and Claude)")
            for line in warnings:
                log.warning("preflight %s", line)
        else:
            problems = [line.strip() for line in lines if line.strip().startswith(("FAIL", "WARN", "Traceback"))]
            log.error("preflight FAILED. Fix before the demo:\n  %s", "\n  ".join(problems or lines[-5:]))

    threading.Thread(target=run, name="preflight", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format=_LOG_FMT, datefmt=_LOG_DATE)
    _stamp_logs()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _run_policy_tests()
    _load_state()
    app.state.awx = AirwallexClient()
    app.state.agent = Copilot()
    log.info("llm %s", app.state.agent.model)
    _run_preflight()
    yield
    await app.state.awx.aclose()


api = FastAPI(title="Payout Copilot", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    # Set when the message was built from uploaded PDFs. One draft per invoice
    # can need one tool round each, so the cap grows by that many, at most 6.
    invoices: int = 0


class InvoiceFile(BaseModel):
    name: str
    data: str


class InvoiceUpload(BaseModel):
    files: list[InvoiceFile]


class ApproveRequest(BaseModel):
    plan_id: str
    fingerprint: str
    pin: str = ""


@api.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@api.get("/api/http")
async def http_trace(after: int = 0):
    """Redacted outbound Airwallex and Anthropic calls for the side panel."""
    return {"calls": http_trace_since(after), "last": http_trace_latest()}


@api.post("/api/http/clear")
async def http_trace_clear():
    clear_http_trace()
    return {"ok": True}


@api.get("/api/next-invoice")
async def invoice_for_chip(supplier: str):
    """The smallest INV-n this supplier has never used. Read only; the chip still goes to Claude."""
    _require_unlocked()
    match, _candidates = resolve(supplier)
    if match is None:
        raise HTTPException(status_code=404, detail=f"{supplier} isn't on the supplier list.")
    try:
        views = await _invoice_views(api.state.awx)
    except AirwallexError as exc:
        raise HTTPException(status_code=502, detail=_err(exc)) from exc
    return {"supplier": match.name, "invoice": next_invoice(views, match)}


@api.post("/api/invoices/read")
async def read_invoices(req: InvoiceUpload):
    """Pull supplier, amount, currency and invoice number out of each PDF.

    Reads only. The chat message it returns goes through /api/chat like a
    typed one, so drafting, policy and approval are unchanged.
    """
    _require_unlocked()
    if not req.files:
        raise HTTPException(status_code=400, detail="Attach at least one PDF invoice.")
    if len(req.files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"I can read up to {MAX_FILES} invoices at a time.")
    invoices = []
    seen = set()
    for item in req.files:
        name = (item.name or "invoice.pdf").strip()[:120]
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            invoices.append({"file": name, "ok": False, "error": f"{name} couldn't be opened.", "missing": []})
            continue
        result = read_invoice(name, data)
        key = (result.get("supplier"), (result.get("invoice") or "").lower())
        if result["ok"] and key in seen:
            result = {**result, "ok": False, "error": f"{name} is {result['invoice']} again, so I only kept it once."}
        elif result["ok"]:
            seen.add(key)
        invoices.append(result)
    _audit(
        "invoices_read",
        {"files": len(invoices), "readable": sum(1 for inv in invoices if inv["ok"])},
    )
    log.info("invoices read %s of %s", sum(1 for inv in invoices if inv["ok"]), len(invoices))
    return {"invoices": invoices, "message": payout_message(invoices)}


@api.get("/api/balances")
async def balances():
    """Live wallet, read when a receipt shows the before-and-after amounts."""
    try:
        rows = await api.state.awx.balances()
    except AirwallexError as exc:
        raise HTTPException(status_code=502, detail=_err(exc)) from exc
    return {"balances": {k: str(v) for k, v in rows.items() if v > 0}}


@api.get("/api/context")
async def context():
    """Lock state and any plan still waiting. Does not call Airwallex."""
    return {
        "pending": [_render(p) for p in _ordered_plans()],
        "locked": ACCOUNT_LOCKED,
        "lock_message": _lock_message() if ACCOUNT_LOCKED else "",
    }


def _is_payoutish(ping: str) -> bool:
    """A message that names an amount or starts as a payment is not a lookup."""
    if any(ch.isdigit() for ch in ping):
        return True
    return ping.startswith(("pay ", "send ", "transfer ", "wire "))


class ToolSession:
    """Live Airwallex reads plus draft_payout. Has no create_transfer."""

    def __init__(self, message: str = ""):
        self.message = message
        self.plans: list[PayoutPlan] = []
        self.blocked: Optional[PolicyRejection] = None
        self.duplicate: Optional[tuple] = None
        self.cancel_chosen: list[dict] = []
        self.cancel_options: list[dict] = []
        self.cancel_question: str = ""
        self.lookups: dict = {}
        self.working: Optional[dict] = None
        self.invoice_views: Optional[list] = None
        self.api_error: Optional[str] = None

    async def get_balances(self) -> dict:
        body = await _lookup("balances")
        self.lookups["balances"] = body
        if isinstance(body, str):
            return {"ok": False, "error": body}
        return {
            "ok": True,
            "balances": [
                {"currency": row.get("label"), "available": row.get("value")}
                for row in body.get("rows") or []
            ],
        }

    async def list_suppliers(self) -> dict:
        body = await _lookup("suppliers")
        self.lookups["suppliers"] = body
        return {
            "ok": True,
            "suppliers": [
                {
                    "name": row.get("label"),
                    "corridor": row.get("value"),
                    "new": bool(row.get("tag")),
                }
                for row in (body.get("rows") or [] if isinstance(body, dict) else [])
            ],
        }

    async def list_transfers(self, query: TransferQuery) -> dict:
        body = await _transfer_history(query, watch=False)
        self.lookups["transfers"] = body
        if isinstance(body, str):
            return {"ok": True, "message": body, "transfers": []}
        return {
            "ok": True,
            "transfers": [
                {
                    "id": row.get("id"),
                    "payee": row.get("label"),
                    "status": row.get("value"),
                    "when": row.get("when"),
                    "amount": row.get("money"),
                    "invoice": row.get("invoice"),
                }
                for row in body.get("rows") or []
            ],
        }

    async def draft_payout(self, intent: PayoutIntent) -> dict:
        blocked = _supplier_block(intent)
        if blocked is not None:
            self.blocked = blocked
            return {
                "ok": False,
                "kind": "blocked",
                "reason": str(blocked),
                "question": blocked.question,
                "sent": False,
            }
        reference = (intent.reference or "").strip()
        view = None
        if reference:
            try:
                if self.invoice_views is None:
                    self.invoice_views = await _invoice_views(api.state.awx)
            except AirwallexError as exc:
                self.api_error = _err(exc)
                return {"ok": False, "error": self.api_error, "sent": False}
            view = already_sent(self.invoice_views, reference, intent.supplier_query)
        if view is not None:
            self.duplicate = (view, reference)
            return {
                "ok": False,
                "kind": "duplicate",
                "reason": _duplicate_line(view, reference),
                "id": view.get("id"),
                "status": view.get("status"),
                "sent": False,
            }
        try:
            if self.working is None:
                self.working = dict(await api.state.awx.balances())
            plan = await _use_live_swift_fee(build_plan(intent, self.working))
        except PolicyRejection as exc:
            self.blocked = exc
            return {
                "ok": False,
                "kind": "blocked",
                "reason": str(exc),
                "question": exc.question,
                "sent": False,
            }
        except AirwallexError as exc:
            self.api_error = _err(exc)
            return {"ok": False, "error": self.api_error, "sent": False}
        self.plans.append(plan)
        debit = plan.total_debit if plan.fee_currency == plan.currency else plan.amount
        self.working[plan.currency] = self.working.get(plan.currency, Decimal("0")) - debit
        if plan.fee and plan.fee_currency != plan.currency:
            self.working[plan.fee_currency] = self.working.get(plan.fee_currency, Decimal("0")) - plan.fee
        shown = _render(plan)
        return {
            "ok": True,
            "kind": "receipt",
            "sent": False,
            "status": "held_for_approval",
            "receipt": {
                "to": shown["supplier"],
                "amount": f"{shown['amount']} {shown['currency']}",
                "fee": f"{shown['fee']} {shown['fee_currency']}",
                "total_debit": f"{shown['total_debit']} {shown['currency']}",
                "rail": shown["corridor"],
                "invoice": shown["reference"],
                "reasons": shown["reasons"],
            },
            "note": "Nothing has been sent. A yes and a PIN are required.",
        }

    async def get_transfer(self, transfer_id: str) -> dict:
        if not transfer_id:
            return {"ok": False, "error": "Which transfer?", "sent": False}
        try:
            raw = await api.state.awx.transfer(transfer_id)
        except AirwallexError as exc:
            return {"ok": False, "error": _err(exc), "sent": False}
        view = view_transfer(raw)
        self.lookups["transfers"] = _history_card(
            [view], "Here's that transfer.", "", watch=False
        )
        return {
            "ok": True,
            "sent": False,
            "transfer": {
                "id": view.get("id"),
                "payee": view.get("payee"),
                "status": view.get("status"),
                "amount": f"{view.get('amount')} {view.get('currency')}",
                "invoice": view.get("reference"),
                "created_at": view.get("created_at"),
            },
        }

    async def draft_cancel(self, query) -> dict:
        try:
            items = await api.state.awx.list_transfers(page_size=30)
        except AirwallexError as exc:
            self.api_error = _err(exc)
            return {"ok": False, "error": self.api_error, "sent": False}
        # "Cancel the last one" means the newest transfer, even if the model
        # copied an older id out of the chat history.
        queries = resolve_queries(self.message, [query]) or [query]
        chosen, question, options = propose(items, queries)
        if options:
            self.cancel_options = options
            self.cancel_question = question or "Which should I cancel?"
            return {
                "ok": False,
                "kind": "unclear",
                "question": self.cancel_question,
                "matches": [_public_transfer(item) for item in options],
                "sent": False,
            }
        if question or not chosen:
            self.cancel_question = question or "Which transfer should I cancel?"
            return {
                "ok": False,
                "kind": "unclear",
                "question": self.cancel_question,
                "sent": False,
            }
        self.cancel_chosen = chosen
        return {
            "ok": True,
            "kind": "cancel_draft",
            "sent": False,
            "transfers": [_public_transfer(item) for item in chosen],
            "note": "Nothing is cancelled until the operator says yes.",
        }


def _public_transfer(view: dict) -> dict:
    return {
        "id": view.get("id"),
        "payee": view.get("payee"),
        "status": view.get("status"),
        "amount": f"{view.get('amount')} {view.get('currency')}",
        "invoice": view.get("reference"),
    }


async def _finish_turn(req: ChatRequest, turn, host: ToolSession) -> dict:
    """Map tool results onto the existing chat cards. Still never sends."""
    if turn.stop_reason == "max_rounds":
        _remember(req.session_id, "assistant", turn.text)
        return {"type": "question", "question": turn.text}
    if host.api_error and not host.plans and host.blocked is None and host.duplicate is None:
        raise HTTPException(status_code=502, detail=host.api_error)
    # One bad payout holds the whole message. The model's reply may claim the
    # others were drafted, so the words here come from code.
    held_back = " Nothing from that message is waiting for approval." if host.plans else ""
    if host.duplicate is not None:
        prior, reference = host.duplicate
        body = _duplicate_invoice(prior, reference)
        body["lead"] += held_back
        return _answered(req, body, user_already=True)
    if host.blocked is not None:
        spoken = (host.blocked.question or "") + held_back
        _remember(req.session_id, "assistant", f"{host.blocked} {spoken}".strip())
        _audit("blocked", {"message": req.message, "reason": str(host.blocked)})
        log.info("blocked -> %s", host.blocked)
        return {"type": "blocked", "reason": str(host.blocked), "question": spoken.strip()}
    if host.plans:
        global CANCEL_BATCH
        CANCEL_BATCH = None
        _replace_plans(host.plans)
        plan = host.plans[0]
        names = ", ".join(item.supplier.name for item in host.plans)
        summary = (
            f"Plan: {plan.amount} {plan.currency} to {plan.supplier.name} "
            f"({plan.supplier.transfer_method}), fingerprint {plan.fingerprint}"
            + (f". Queue: {names}" if len(host.plans) > 1 else "")
        )
        _remember(req.session_id, "assistant", summary)
        for item in host.plans:
            _audit(
                "planned",
                {
                    "plan_id": item.plan_id,
                    "supplier": item.supplier.name,
                    "amount": f"{item.amount} {item.currency}",
                    "decision": item.decision.value,
                },
            )
            log.info("plan %s %s %s", item.supplier.name, item.amount, item.currency)
        return {
            "type": "plan",
            "plan": _render(plan),
            "plans": [_render(item) for item in host.plans],
            "queue": len(host.plans),
            "confidence": 1.0,
            "lead": turn.text,
        }

    if host.cancel_chosen:
        return _hold_cancel(req, host.cancel_chosen, turn.text)
    if host.cancel_options:
        body = _history_card(
            host.cancel_options,
            "These are the open ones.",
            "",
            watch=False,
        )
        body["lead"] = turn.text or host.cancel_question
        return _answered(req, body, user_already=True)
    if host.cancel_question:
        spoken = turn.text or host.cancel_question
        _remember(req.session_id, "assistant", spoken)
        return {"type": "question", "question": spoken}

    for kind in ("transfers", "balances", "suppliers"):
        if kind not in host.lookups:
            continue
        body = host.lookups[kind]
        if isinstance(body, dict) and body.get("type") == "list":
            body = dict(body)
            if turn.text:
                body["lead"] = turn.text
            body["note"] = ""
        elif isinstance(body, dict) and body.get("type") == "info" and turn.text:
            body = dict(body, text=turn.text)
        _audit("lookup", {"message": req.message, "lookup": kind})
        return _answered(req, body, user_already=True)

    question = turn.text or "I didn't quite catch that. Who should I pay, how much, and in which currency?"
    _remember(req.session_id, "assistant", question)
    _audit("clarification", {"message": req.message, "question": question})
    log.info("clarify -> %s", question)
    return {"type": "question", "question": question}


_OFFLINE = "I couldn't reach Claude just now, so this answer is a shortcut."


def _with_notice(body, notice: str):
    if isinstance(body, str):
        return {"type": "info", "text": f"{notice}\n{body}"}
    if not isinstance(body, dict):
        return body
    body = dict(body)
    if body.get("type") == "list":
        body["lead"] = notice
    elif body.get("type") == "question":
        body["question"] = f"{notice} {body.get('question') or ''}".strip()
    elif body.get("text"):
        body["text"] = f"{notice}\n{body['text']}"
    else:
        body["lead"] = notice
    return body


async def _shortcut(req: ChatRequest, user_already: bool = False):
    """Answers when Claude is unreachable. Every message tries Claude first."""
    ping = req.message.strip().lower()
    payoutish = _is_payoutish(ping)
    if ping in {"?", "hello", "hello?", "hi", "hey", "are you there"}:
        text = "Hey, I'm here. Tell me who to pay, or ask for balances, suppliers, or history."
        if not user_already:
            _remember(req.session_id, "user", req.message)
        _remember(req.session_id, "assistant", text)
        return {"type": "info", "text": text}
    if not payoutish and ("supplier" in ping or ping in {"who can i pay", "who can we pay"}):
        return _answered(req, await _lookup("suppliers"), user_already)
    if not payoutish and "balance" in ping:
        return _answered(req, await _lookup("balances"), user_already)
    if not payoutish and ping.rstrip("?.!") in {
        "history",
        "payout history",
        "transaction history",
        "what's the history",
        "whats the history",
        "what's the payout history",
        "whats the payout history",
    }:
        return _answered(req, await _transfer_history(), user_already)
    if looks_like_cancel(req.message) and not _asks_for_history(req.message):
        queries = resolve_queries(req.message, None)
        if not user_already:
            _remember(req.session_id, "user", req.message)
        if not queries:
            question = "Which transfer should I cancel?"
            _remember(req.session_id, "assistant", question)
            return {"type": "question", "question": question}
        return await _propose_cancel(req, queries)
    guess = None if payoutish else _guess_transfer_query(req.message)
    if guess is not None:
        return _answered(req, await _transfer_history(guess), user_already)
    return None


@api.post("/api/chat")
async def chat(req: ChatRequest):
    """Every message, typed or a chip, goes to Claude."""
    _require_unlocked()
    history = list(SESSIONS[req.session_id])
    log.info("chat [%s] %r", req.session_id[:8], req.message)
    ping = req.message.strip().lower()
    if not ping:
        raise HTTPException(status_code=400, detail="Who should I pay, and for how much?")
    _remember(req.session_id, "user", req.message)
    host = ToolSession(req.message)
    rounds = MAX_TOOL_ROUNDS + max(0, min(MAX_FILES, req.invoices))
    turn = await api.state.agent.run(req.message, history=history, host=host, max_rounds=rounds)
    if turn.stop_reason == "unavailable":
        hit = await _shortcut(req, user_already=True)
        if hit is not None:
            return _with_notice(hit, _OFFLINE)
        return {
            "type": "question",
            "question": (
                f"{_OFFLINE} Ask for balances, suppliers, or history, or try me again."
            ),
        }
    return await _finish_turn(req, turn, host)


def _check_pin(plan_id: str, pin: str) -> None:
    """Three wrong PINs reject the plan and lock the account. The default demo PIN is 1234."""
    global ACCOUNT_LOCKED, PIN_MISSES
    expected = os.getenv("APPROVAL_PIN", "1234")
    given = (pin or "").strip()
    ok = len(given) == 4 and given.isdigit() and secrets.compare_digest(given, expected)
    if ok:
        if PIN_MISSES:
            PIN_MISSES = 0
            _save_state()
        return
    PIN_MISSES += 1
    if PIN_MISSES >= PIN_LIMIT:
        ACCOUNT_LOCKED = True
        PLANS.clear()
        PLAN_ORDER.clear()
        PIN_MISSES = 0
        _audit("locked", {"plan_id": plan_id, "reason": "incorrect_pin"})
        raise HTTPException(status_code=403, detail=_lock_message())
    _audit("wrong_pin", {"plan_id": plan_id, "misses": PIN_MISSES})
    left = PIN_LIMIT - PIN_MISSES
    raise HTTPException(
        status_code=401,
        detail=(
            f"That PIN isn't right. {left} attempt{'s' if left != 1 else ''} "
            "left before the account locks."
        ),
    )


def _tracked(result: dict) -> dict:
    """Sandbox payouts walk PROCESSING → SENT → PAID on a fixed timer."""
    transfer = result.get("transfer") or {}
    status = (transfer.get("status") or "").upper()
    transfer_id = transfer.get("transfer_id") or ""
    if transfer_id and status not in {"PAID", "CANCELLED", "FAILED"}:
        start_sandbox_progress(api.state.awx, transfer_id)
    return result


def _duplicate(exc: AirwallexError) -> bool:
    return "duplicate_request" in f"{exc.code or ''} {exc}".lower()


def _with_charges(transfer: dict, body: dict) -> dict:
    """Keep the fee Airwallex actually charged, not the estimate on the plan."""
    raw = body.get("raw") if isinstance(body.get("raw"), dict) else body
    pays = raw.get("amount_payer_pays")
    if pays is None:
        return transfer
    transfer = dict(transfer)
    transfer["amount_payer_pays"] = pays
    transfer["fee_amount"] = raw.get("fee_amount")
    transfer["fee_currency"] = raw.get("fee_currency")
    return transfer


_SWIFT_FEE_CACHE: dict[str, tuple[float, Decimal]] = {}


def _note_swift_fee(items: list) -> None:
    """Remember the newest SWIFT fee already returned by a transfer list."""
    for item in items:
        if (item.get("transfer_method") or "").upper() != "SWIFT":
            continue
        if (item.get("fee_currency") or "").upper() != "EUR":
            continue
        try:
            fee = Decimal(str(item.get("fee_amount")))
        except (InvalidOperation, TypeError):
            continue
        if fee > 0:
            _SWIFT_FEE_CACHE["EUR"] = (time.monotonic(), fee)
            return


def _quoted_swift_fee() -> Optional[Decimal]:
    """Fee seen on the transfer list used for the invoice check. No extra request."""
    cached = _SWIFT_FEE_CACHE.get("EUR")
    if cached and time.monotonic() - cached[0] < 120:
        return cached[1]
    return None


async def _use_live_swift_fee(plan: PayoutPlan) -> PayoutPlan:
    """Price a SWIFT payout with the fee Airwallex is charging now."""
    if plan.supplier.transfer_method != "SWIFT" or plan.fee_currency != plan.currency:
        return plan
    quoted = _quoted_swift_fee()
    if quoted is None or quoted == plan.fee:
        return plan
    plan.fee = quoted
    plan.total_debit = plan.amount + quoted
    if plan.total_debit > plan.available_balance:
        raise PolicyRejection(
            f"There isn't enough {plan.currency} for this. It needs {plan.total_debit}, "
            f"and the wallet has {plan.available_balance}."
        )
    plan.reasons = [
        f"SWIFT adds a flat {quoted} {plan.fee_currency} fee."
        if reason.startswith("SWIFT adds a flat")
        else reason
        for reason in plan.reasons
    ]
    plan.fingerprint = fingerprint(plan)
    return plan


def _finish(plan: PayoutPlan, body: dict, *, duplicate: bool) -> dict:
    transfer = body if "transfer_id" in body else _transfer_result(body).model_dump()
    transfer = _with_charges(transfer, body)
    mark_paid(plan.supplier.id, plan.supplier.beneficiary_id)
    PLANS.pop(plan.plan_id, None)
    if plan.plan_id in PLAN_ORDER:
        PLAN_ORDER.remove(plan.plan_id)
    nxt = _ordered_plans()
    _audit(
        "duplicate_avoided" if duplicate else "executed",
        {
            "plan_id": plan.plan_id,
            "transfer_id": transfer.get("transfer_id"),
            "status": transfer.get("status"),
            "amount": f"{plan.amount} {plan.currency}",
        },
    )
    return {
        "type": "executed",
        "plan_id": plan.plan_id,
        "duplicate_avoided": duplicate,
        "transfer": transfer,
        "request_id": plan.request_id,
        "next": _render(nxt[0]) if nxt else None,
        "remaining": len(nxt),
    }


def _drop_plan(plan_id: str) -> None:
    PLANS.pop(plan_id, None)
    if plan_id in PLAN_ORDER:
        PLAN_ORDER.remove(plan_id)


async def _invoice_views(awx: AirwallexClient) -> list[dict]:
    """Newest transfers, a few pages at most. Enough to catch a repeated invoice."""
    views: list[dict] = []
    for page in range(3):
        items, has_more = await awx.list_transfer_page(page_size=100, page_num=page)
        if page == 0:
            _note_swift_fee(items)
        views.extend(view_transfer(item) for item in items if item.get("id"))
        if not has_more:
            break
    return views


def _supplier_block(intent: PayoutIntent) -> PolicyRejection | None:
    """Directory lookup before any invoice check or pricing."""
    supplier, _candidates = resolve(intent.supplier_query)
    if supplier is not None:
        return None
    try:
        build_plan(intent, {})
    except PolicyRejection as exc:
        return exc
    return None


async def _sent_invoice(awx: AirwallexClient, reference: str, supplier_query: str) -> dict | None:
    if not (reference or "").strip() or not (supplier_query or "").strip():
        return None
    return already_sent(await _invoice_views(awx), reference, supplier_query)


def _duplicate_line(view: dict, reference: str) -> str:
    money = f"{_amt(view['amount'])} {view['currency']}"
    status = str(view.get("status") or "sent").lower()
    return (
        f"{reference} already went out to {view['payee']} ({money}, {status}). "
        "I won't send it again."
    )


def _duplicate_invoice(view: dict, reference: str) -> dict:
    body = _history_card([view], "Here's that invoice.", "Want me to create another invoice?", watch=False)
    lead = _duplicate_line(view, reference)
    body["lead"] = lead
    body["text"] = f"{lead}\n{body.get('text') or ''}"
    _audit("duplicate_invoice", {"reference": reference, "id": view.get("id"), "status": view.get("status")})
    log.info("duplicate invoice %s -> %s", reference, view.get("id"))
    return body


async def _execute(plan: PayoutPlan, awx: AirwallexClient) -> dict:
    """Send a plan. A retry adopts an existing transfer instead of paying twice."""
    try:
        existing = await awx.find_transfer_by_request_id(plan.request_id)
    except AirwallexError as exc:
        raise HTTPException(status_code=502, detail=_err(exc)) from exc
    if existing:
        return _tracked(_finish(plan, existing, duplicate=True))

    try:
        prior = await _sent_invoice(awx, plan.reference, plan.supplier.name)
    except AirwallexError as exc:
        raise HTTPException(status_code=502, detail=_err(exc)) from exc
    if prior is not None:
        _drop_plan(plan.plan_id)
        raise HTTPException(status_code=409, detail=_duplicate_line(prior, plan.reference))

    try:
        beneficiary_id = plan.supplier.beneficiary_id
        if not beneficiary_id:
            await awx.validate_beneficiary(plan.supplier)
            beneficiary_id = await awx.create_beneficiary(
                plan.supplier, request_id=f"ben-{plan.request_id}"
            )
            plan.supplier.beneficiary_id = beneficiary_id
            live = by_id(plan.supplier.id)
            if live is not None and not live.beneficiary_id:
                live.beneficiary_id = beneficiary_id
            _save_state()
            log.info("created beneficiary %s for %s", beneficiary_id, plan.supplier.name)

        result = await awx.create_transfer(
            request_id=plan.request_id,
            beneficiary_id=beneficiary_id,
            amount=plan.amount,
            currency=plan.currency,
            reference=plan.reference,
            transfer_method=plan.supplier.transfer_method,
        )
    except AirwallexError as exc:
        if _duplicate(exc):
            try:
                existing = await awx.find_transfer_by_request_id(plan.request_id)
            except AirwallexError:
                existing = None
            if existing:
                return _tracked(_finish(plan, existing, duplicate=True))
        _audit("execution_failed", {"plan_id": plan.plan_id, "error": _err(exc)})
        raise HTTPException(status_code=502, detail=_err(exc)) from exc

    return _tracked(_finish(plan, result.model_dump(), duplicate=False))


@api.post("/api/approve")
async def approve(req: ApproveRequest):
    """Execute a plan against a human's consent.

    Two guards before any API call: the plan must still exist, and the
    fingerprint the client approved must match the plan as it stands now.
    """
    _require_unlocked()
    plan = PLANS.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Unknown or already-executed plan.")
    if plan.plan_id in IN_FLIGHT:
        raise HTTPException(status_code=409, detail="This payout is already being sent.")
    _check_pin(plan.plan_id, req.pin)
    _require_shown(plan, req.fingerprint)

    IN_FLIGHT.add(plan.plan_id)
    awx: AirwallexClient = api.state.awx
    try:
        return await _execute(plan, awx)
    finally:
        IN_FLIGHT.discard(plan.plan_id)


def _require_shown(plan: PayoutPlan, shown: str) -> None:
    """The fingerprint the operator approved must match the plan as it stands now."""
    if shown == plan.fingerprint:
        return
    recomputed = fingerprint(plan)
    _audit(
        "fingerprint_mismatch",
        {
            "plan_id": plan.plan_id,
            "shown": plan.fingerprint,
            "sent": shown,
            "recomputed": recomputed,
        },
    )
    log.warning(
        "fingerprint mismatch plan=%s shown=%s sent=%s recomputed=%s",
        plan.plan_id,
        plan.fingerprint,
        shown,
        recomputed,
    )
    raise HTTPException(
        status_code=409,
        detail=f"The {plan.supplier.name} payout changed since it was shown to you. Review it again.",
    )


class ApprovedItem(BaseModel):
    plan_id: str
    fingerprint: str


class BatchApproveRequest(BaseModel):
    items: list[ApprovedItem]
    pin: str = ""


@api.post("/api/approve-batch")
async def approve_batch(req: BatchApproveRequest):
    """One PIN for payouts the operator said yes to one by one.

    Every plan and fingerprint is checked before the first send. Sends run
    in queue order, one at a time, and stop at the first failure.
    """
    _require_unlocked()
    ids = [item.plan_id for item in req.items]
    if not ids or len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Nothing to approve.")
    shown = {item.plan_id: item.fingerprint for item in req.items}
    plans = [plan for plan in _ordered_plans() if plan.plan_id in shown]
    if len(plans) != len(ids):
        raise HTTPException(status_code=404, detail="One of these payouts is no longer waiting. Nothing was sent.")
    if any(plan.plan_id in IN_FLIGHT for plan in plans):
        raise HTTPException(status_code=409, detail="These payouts are already being sent.")
    _check_pin(plans[0].plan_id, req.pin)
    for plan in plans:
        _require_shown(plan, shown[plan.plan_id])

    awx: AirwallexClient = api.state.awx
    IN_FLIGHT.update(ids)
    results: list[dict] = []
    failed = None
    try:
        for plan in plans:
            try:
                results.append(await _execute(plan, awx))
            except HTTPException as exc:
                failed = {"plan_id": plan.plan_id, "supplier": plan.supplier.name, "error": exc.detail}
                break
    finally:
        IN_FLIGHT.difference_update(ids)
    _audit("batch_approved", {"plans": ids, "sent": len(results), "failed": failed and failed["plan_id"]})
    return {
        "type": "executed_batch",
        "results": results,
        "failed": failed,
        "remaining": len(_ordered_plans()),
    }


@api.post("/api/admin/reset")
async def admin_reset():
    """Hidden unlock. The title click is the only control that calls this."""
    global ACCOUNT_LOCKED, PIN_MISSES
    was_locked = ACCOUNT_LOCKED
    ACCOUNT_LOCKED = False
    PIN_MISSES = 0
    if was_locked:
        _audit("unlocked", {})
    else:
        _save_state()
    return {"type": "reset", "was_locked": was_locked}


@api.post("/api/reject")
async def reject(req: ApproveRequest):
    _require_unlocked()
    if req.plan_id in IN_FLIGHT:
        raise HTTPException(status_code=409, detail="This payout is already being sent.")
    queued = len(_ordered_plans())
    PLANS.clear()
    PLAN_ORDER.clear()
    _audit("rejected", {"plan_id": req.plan_id})
    return {"type": "rejected", "cleared": queued}


class CancelConfirm(BaseModel):
    batch_id: str
    fingerprint: str
    confirm: bool = True


async def _propose_cancel(req: ChatRequest, queries: list) -> dict:
    """List real transfers, match them, and wait for a yes. Does not cancel."""
    global CANCEL_BATCH
    try:
        items = await api.state.awx.list_transfers(page_size=30)
    except AirwallexError as exc:
        raise HTTPException(status_code=502, detail=_err(exc)) from exc
    chosen, question, options = propose(items, queries)
    if options:
        text = question or "Which should I cancel?"
        _remember(req.session_id, "assistant", text)
        _audit("cancel_unclear", {"message": req.message, "question": text})
        body = _history_card(
            options, "These are the open ones.", "Tell me the invoice, or say the latest one.", watch=False
        )
        body["lead"] = text
        return body
    if question or not chosen:
        text = question or "Which transfer should I cancel?"
        _remember(req.session_id, "assistant", text)
        _audit("cancel_unclear", {"message": req.message, "question": text})
        return {"type": "question", "question": text}
    return _hold_cancel(req, chosen, "")


def _hold_cancel(req: ChatRequest, chosen: list[dict], lead: str) -> dict:
    """Store the matched transfers and wait for yes. Does not cancel."""
    global CANCEL_BATCH
    batch_id = str(uuid.uuid4())
    CANCEL_BATCH = {
        "batch_id": batch_id,
        "fingerprint": cancel_fingerprint(chosen),
        "transfers": chosen,
    }
    summary = lead or (
        "Cancel " + ", ".join(
            f"{item['payee']} {item['amount']} {item['currency']}" for item in chosen
        )
    )
    _remember(req.session_id, "assistant", summary)
    _audit("cancel_proposed", {"ids": [item["id"] for item in chosen]})
    log.info("cancel proposed %s", summary)
    return {
        "type": "cancel",
        "batch_id": batch_id,
        "fingerprint": CANCEL_BATCH["fingerprint"],
        "transfers": chosen,
        "lead": lead,
    }


@api.post("/api/cancel-transfers")
async def cancel_transfers(req: CancelConfirm):
    """Cancel the transfers the user just confirmed. Nothing else."""
    global CANCEL_BATCH
    _require_unlocked()
    batch = CANCEL_BATCH
    if batch is None or batch["batch_id"] != req.batch_id:
        raise HTTPException(
            status_code=404,
            detail="That cancel isn't waiting anymore. Ask me again and I'll look it up.",
        )
    if req.fingerprint != batch["fingerprint"]:
        raise HTTPException(
            status_code=409,
            detail="Those transfers changed since I showed them. Ask me again.",
        )
    CANCEL_BATCH = None
    if not req.confirm:
        _audit("cancel_declined", {"batch_id": req.batch_id})
        return {"type": "declined"}

    results = []
    failed = []
    for item in batch["transfers"]:
        try:
            body = await api.state.awx.cancel_transfer(item["id"])
        except AirwallexError as exc:
            failed.append({**item, "ok": False, "error": _cancel_error(item, exc)})
            _audit("cancel_failed", {"id": item["id"], "error": exc.code or "error"})
            continue
        results.append({**item, "ok": True, "status": body.get("status") or "CANCELLED"})
        _audit("cancelled", {"id": item["id"], "amount": f"{item['amount']} {item['currency']}"})
    log.info("cancelled %s failed %s", len(results), len(failed))
    return {"type": "cancelled", "results": results, "failed": failed}


def _cancel_error(item: dict, exc: AirwallexError) -> str:
    who = f"{item.get('payee') or 'That transfer'} {item.get('amount')} {item.get('currency')}".strip()
    if exc.code == "update_unsupported":
        return f"Airwallex won't cancel {who}. It's already too far along."
    if exc.code == "transfer_not_found":
        return f"Airwallex couldn't find {who}."
    return f"I couldn't cancel {who}. {_err(exc)}"


def _resume_sandbox(transfer_id: str, status: str) -> None:
    """The timer lives in memory. A reload loses it, so a status read restarts it."""
    if transfer_id and step_for(str(status or "").upper()):
        start_sandbox_progress(api.state.awx, transfer_id)


@api.get("/api/transfers/recent")
async def recent_transfers():
    """Latest eight transfers, status only. One call, shared by the history bars."""
    _require_unlocked()
    try:
        items = await api.state.awx.list_transfers(page_size=8)
    except AirwallexError as exc:
        raise HTTPException(status_code=502, detail=_err(exc)) from exc
    transfers = []
    for raw in items:
        view = view_transfer(raw)
        _resume_sandbox(view["id"], view["status"])
        transfers.append({
            "id": view["id"],
            "status": view["status"],
            "countdown": countdown(view["id"]) if view["id"] else None,
            "failure_type": raw.get("failure_type"),
            "failure_reason": raw.get("failure_reason"),
        })
    return {"transfers": transfers}


@api.get("/api/transfer/{transfer_id}")
async def transfer_status(transfer_id: str):
    """Read a transfer's current state.

    A payout that does not complete ends in CANCELLED, not FAILED, and
    CANCELLED does not mean a person cancelled it — so both are treated as
    terminal failures and failure_type is what explains why.
    """
    try:
        body = await api.state.awx.transfer(transfer_id)
    except AirwallexError as exc:
        raise HTTPException(status_code=502, detail=_err(exc)) from exc

    status = body.get("status", "UNKNOWN")
    _resume_sandbox(transfer_id, status)
    return {
        "status": status,
        "terminal": status in {"PAID", "CANCELLED", "FAILED"},
        "failed": status in {"CANCELLED", "FAILED"},
        "failure_type": body.get("failure_type"),
        "failure_reason": body.get("failure_reason"),
        "countdown": countdown(transfer_id),
    }


def _ordered_plans() -> list[PayoutPlan]:
    ordered = [PLANS[pid] for pid in PLAN_ORDER if pid in PLANS]
    ordered.extend(plan for pid, plan in PLANS.items() if pid not in PLAN_ORDER)
    return ordered


def _replace_plans(plans: list[PayoutPlan]) -> None:
    PLANS.clear()
    PLAN_ORDER.clear()
    for plan in plans:
        PLANS[plan.plan_id] = plan
        PLAN_ORDER.append(plan.plan_id)
    _save_state()


def _render(plan: PayoutPlan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "fingerprint": plan.fingerprint,
        "supplier": plan.supplier.name,
        "corridor": (
            f"{plan.supplier.country_code} / {plan.currency} / {plan.supplier.transfer_method}"
        ),
        "amount": str(plan.amount),
        "currency": plan.currency,
        "fee": str(plan.fee),
        "fee_currency": plan.fee_currency,
        "total_debit": str(plan.total_debit),
        "available_balance": str(plan.available_balance),
        "reference": plan.reference,
        "decision": plan.decision.value,
        "reasons": plan.reasons,
        "pin_attempts_left": PIN_LIMIT - PIN_MISSES,
    }


def _asks_for_history(message: str) -> bool:
    low = message.lower()
    if any(word in low for word in ("cancel", "stop", "void", "undo")):
        return False
    sees = any(word in low for word in ("transfer", "payout", "payment", "history"))
    asks = any(word in low for word in ("last", "latest", "newest", "most recent", "today", "yesterday", "between", "history"))
    return sees and asks


def _guess_transfer_query(message: str) -> Optional[TransferQuery]:
    """Backup when the model misses a plain 'last transfer' question."""
    low = " ".join(message.lower().split())
    if any(word in low for word in ("cancel", "stop", "void", "undo")):
        return None
    if not any(word in low for word in ("transfer", "payout", "payment")):
        return None
    if not any(word in low for word in ("last", "latest", "newest", "most recent")):
        return None
    limit = 1
    for word, count in (("two", 2), ("three", 3), ("four", 4), ("five", 5)):
        if f"last {word}" in low or f"latest {word}" in low:
            limit = count
    return TransferQuery(scope="latest", limit=limit)


def _as_local(raw: str) -> Optional[datetime]:
    text = (raw or "").strip()
    if not text:
        return None
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return moment


def _bound(raw: str, end: bool) -> Optional[datetime]:
    text = (raw or "").strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = text + ("T23:59:59" if end else "T00:00:00")
    return _as_local(text)


def _select_transfers(views: list[dict], query: TransferQuery) -> list[dict]:
    since_raw = query.since or ""
    until_raw = query.until or ""
    start = _bound(since_raw, end=False)
    finish = _bound(until_raw, end=False)
    if start and finish and start > finish:
        since_raw, until_raw = until_raw, since_raw
    since = _bound(since_raw, end=False)
    until = _bound(until_raw, end=True)
    supplier = (query.supplier_query or "").strip().lower()
    reference = (query.reference or "").strip().lower()
    status = (query.status or "").strip().upper()
    chosen = []
    for view in views:
        payee = (view.get("payee") or "").lower()
        if status and (view.get("status") or "").upper() != status:
            continue
        if supplier and supplier not in payee:
            continue
        if reference and not reference_matches(view, reference):
            continue
        if since or until:
            moment = _as_local(view.get("created_at") or "")
            if moment is None:
                continue
            if since and moment < since:
                continue
            if until and moment > until:
                continue
        chosen.append(view)
    chosen.sort(key=newest_first, reverse=True)
    if query.limit <= 0 or query.scope == "range" or query.since or query.until:
        return chosen
    return chosen[: max(1, query.limit)]


def _page_ends_before(items: list, since: datetime) -> bool:
    """True when a newest-first page has moved entirely before the window."""
    newest = None
    for item in items:
        moment = parse_created_at(item.get("created_at") or "")
        if moment and (newest is None or moment > newest):
            newest = moment
    return newest is not None and newest < since


async def _fetch_transfers(query: Optional[TransferQuery]) -> list:
    """Page GET /transfers until the request is satisfied.

    A date window keeps going until the pages fall before `since` or the list
    ends, so a day with 101 transfers returns 101. "Last N" stops at N.
    """
    window = query is not None and (query.scope == "range" or bool(query.since or query.until))
    want = None if window else max(1, int(query.limit if query else 8))
    since = _bound(query.since, end=False) if query and query.since else None
    items: list = []
    for page in range(50):
        size = 100 if window else min(100, want or 8)
        batch, has_more = await api.state.awx.list_transfer_page(page_size=size, page_num=page)
        items.extend(batch)
        if not has_more or not batch:
            break
        if window and since and _page_ends_before(batch, since):
            break
        if want is not None and len(items) >= want:
            break
    return items


async def _transfer_history(query: Optional[TransferQuery] = None, *, watch: bool = True):
    try:
        items = await _fetch_transfers(query)
    except AirwallexError as exc:
        return f"I couldn't pull the history just now: {_err(exc)}"
    views = [view_transfer(t) for t in items]
    views.sort(key=newest_first, reverse=True)
    if query is not None:
        views = _select_transfers(views, query)
    else:
        views = views[:8]
    if not views:
        if query is not None:
            return "I don't see a transfer in that window. Want a different time, or should I pay someone?"
        return "Nothing has been sent yet. Tell me who to pay whenever you're ready."
    if query is None:
        title = "Here's what has gone out recently."
        note = "Want me to cancel one, or pay someone?"
    elif query.scope == "latest" and len(views) == 1:
        title = "Here's the latest transfer."
        note = "Want me to cancel it, or pay someone else?"
    else:
        title = "Here's what matches."
        note = "Want the details on one of these, or should I cancel one?"
    if not watch:
        note = ""
    return _history_card(views, title, note, watch=watch)


def _history_card(views: list[dict], title: str, note: str = "", watch: bool = True) -> dict:
    rows = []
    for view in views:
        ref = view["reference"] or "No invoice"
        when = _when(view["created_at"])
        money = f"{_amt(view['amount'])} {view['currency']}"
        detail = " · ".join(part for part in (when, money, ref) if part)
        if watch:
            _resume_sandbox(view["id"], view["status"])
        rows.append({
            "label": view["payee"],
            "country": view.get("country") or "",
            "value": view["status"],
            "sub": detail,
            "when": when,
            "money": money,
            "invoice": ref,
            "id": view["id"],
            "copy": True,
        })
    return _list(title, rows, note)


def _remember(session_id: str, role: str, content: str) -> None:
    if not content:
        return
    SESSIONS[session_id].append({"role": role, "content": content})
    SESSIONS[session_id] = SESSIONS[session_id][-12:]
    _save_state()


def _load_state() -> None:
    global ACCOUNT_LOCKED, PIN_MISSES
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return
    ACCOUNT_LOCKED = bool(data.get("locked"))
    try:
        PIN_MISSES = max(0, min(PIN_LIMIT - 1, int(data.get("pin_misses") or 0)))
    except (TypeError, ValueError):
        PIN_MISSES = 0
    for supplier_id, saved in (data.get("suppliers") or {}).items():
        live = by_id(supplier_id)
        if live is None or not isinstance(saved, dict):
            continue
        if saved.get("beneficiary_id"):
            live.beneficiary_id = saved["beneficiary_id"]
        if saved.get("previously_paid"):
            live.previously_paid = True
    for sid, turns in (data.get("sessions") or {}).items():
        SESSIONS[sid] = turns[-12:]
    AUDIT.extend(data.get("audit") or [])
    for raw in data.get("plans") or []:
        try:
            plan = PayoutPlan.model_validate(raw)
            PLANS[plan.plan_id] = plan
        except Exception:
            continue


def _save_state() -> None:
    STATE_PATH.write_text(
        json.dumps(
            {
                "sessions": dict(SESSIONS),
                "audit": AUDIT[-40:],
                "plans": [p.model_dump(mode="json") for p in PLANS.values()],
                "locked": ACCOUNT_LOCKED,
                "pin_misses": PIN_MISSES,
                "suppliers": {
                    s.id: {"beneficiary_id": s.beneficiary_id, "previously_paid": s.previously_paid}
                    for s in all_suppliers()
                    if s.beneficiary_id or s.previously_paid
                },
            },
            indent=2,
            default=str,
        )
    )


def _when(raw: str) -> str:
    """Airwallex timestamps arrive as 2026-10-07T16:39:36+0000. Show local time."""
    if not (raw or "").strip():
        return ""
    moment = parse_created_at(raw)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S") if moment else raw


def _amt(amount) -> str:
    try:
        return f"{Decimal(str(amount)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(amount)


def _list(title: str, rows: list[dict], note: str = "", compact: bool = False, kind: str = "") -> dict:
    lines = [title]
    for row in rows:
        piece = row.get("label") or ""
        if row.get("value"):
            piece += f" — {row['value']}"
        if row.get("sub"):
            piece += f" ({row['sub']})"
        if row.get("id"):
            piece += f" ref {row['id']}"
        if row.get("tag"):
            piece += f" [{row['tag']}]"
        lines.append(piece)
    if note:
        lines.append(note)
    body = {"type": "list", "title": title, "rows": rows, "note": note, "text": "\n".join(lines)}
    if compact:
        body["compact"] = True
    if kind:
        body["kind"] = kind
    return body


def _answered(req: ChatRequest, payload, user_already: bool = False) -> dict:
    """Remember a lookup and return it. Lists render line by line in the chat."""
    if isinstance(payload, str):
        body = {"type": "info", "text": payload}
        remembered = payload
    elif payload.get("type") == "list":
        body = payload
        remembered = _card_memory(payload)
    else:
        body = payload
        remembered = payload.get("text") or ""
    if not user_already:
        _remember(req.session_id, "user", req.message)
    _remember(req.session_id, "assistant", remembered)
    return body


def _card_memory(body: dict) -> str:
    """What the model sees later. Card rows stay out of the history, so the
    model does not learn to repeat them in its own words."""
    said = (body.get("lead") or body.get("title") or "").strip()
    rows = len(body.get("rows") or [])
    card = f"[Showed a card: {body.get('title') or 'details'}, {rows} rows.]"
    return "\n".join(part for part in (said, card, (body.get("note") or "").strip()) if part)


async def _lookup(kind: Optional[str]):
    if kind == "suppliers":
        rows = []
        for s in all_suppliers():
            row = {
                "label": s.name,
                "country": s.country_code,
                "value": f"{s.currency} · {s.transfer_method}",
                "copy": True,
            }
            if not s.previously_paid:
                row["tag"] = "New"
            rows.append(row)
        return _list(
            "These are the suppliers I can pay.",
            rows,
            "Want me to pay one of them?",
            compact=True,
            kind="suppliers",
        )
    try:
        balances = await api.state.awx.balances()
    except AirwallexError as exc:
        return f"I couldn't read the wallet just now: {_err(exc)}"
    rows = [
        {"label": cur, "value": f"{amt:,.2f}", "copy_value": True}
        for cur, amt in balances.items()
        if amt > 0
    ]
    if not rows:
        return "The wallet's empty right now."
    return _list(
        "Here's what's in the wallet.",
        rows,
        "Want me to pay someone, or look up a transfer?",
        compact=True,
    )


def _err(exc: AirwallexError) -> str:
    return f"{exc.code or 'error'}: {exc}"


def _audit(event: str, detail: dict) -> None:
    AUDIT.append({
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        **detail,
    })
    _save_state()


api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app = api
