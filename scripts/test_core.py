#!/usr/bin/env python3
"""Core offline checks. No Airwallex keys, no LLM, no network.

Covers the guardrails, the PIN gate, no double pay, cancel matching, that no
tool can move money, and the sandbox timer. The server runs this at startup.

    .venv/bin/python scripts/test_core.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.main as m  # noqa: E402
from app.agent import MAX_TOOL_ROUNDS, AgentTurn, Copilot  # noqa: E402
from app.cancel import propose, resolve_queries  # noqa: E402
from app.invoices import already_sent, next_invoice  # noqa: E402
from app.models import CancelQuery, Decision, PayoutIntent, TransferResult  # noqa: E402
from app.policy import PolicyRejection, build_plan  # noqa: E402
from app.sandbox_progress import ACME_PROCESSING_DELAY, PAID_DELAY, SENT_DELAY, step_for  # noqa: E402
from app.suppliers import all_suppliers  # noqa: E402
from app.tools import TOOL_NAMES, TOOLS, call_tool  # noqa: E402
from fastapi import HTTPException  # noqa: E402

logging.disable(logging.CRITICAL)
# Never touch the real state.json.
m.STATE_PATH = Path(tempfile.mkdtemp()) / "state.json"
os.environ["AUTO_APPROVE_LIMIT_USD"] = "0"
os.environ["APPROVAL_PIN"] = "1234"

BALANCES = {"EUR": Decimal("20000"), "USD": Decimal("25000"), "GBP": Decimal("8000")}
FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILS += 1
        if detail:
            print(f"        {detail}")


def intent(**kw) -> PayoutIntent:
    base = dict(
        supplier_query="Acme", amount=Decimal("4000"), currency="EUR",
        reference="INV-1032", confidence=1.0, missing=[],
    )
    base.update(kw)
    return PayoutIntent(**base)


def rejected(**kw) -> bool:
    try:
        build_plan(intent(**kw), BALANCES)
    except PolicyRejection:
        return True
    return False


def transfer(tid, status, reference, created, payee="Acme Logistics GmbH", amount="100", currency="EUR"):
    return {
        "id": tid, "status": status, "reference": reference, "created_at": created,
        "transfer_amount": amount, "transfer_currency": currency, "source_currency": currency,
        "transfer_method": "SWIFT", "fee_amount": "12.85", "fee_currency": "EUR",
        "short_reference_id": f"P-{tid[:4]}",
        "beneficiary": {"bank_details": {"account_name": payee}},
    }


class FakeAwx:
    """Records every call. A base URL without 'sandbox' keeps the timer off."""

    base_url = "https://offline.test"

    def __init__(self, items=None, existing=None):
        self.items = list(items or [])
        self.existing = existing
        self.calls: list[str] = []

    async def balances(self):
        self.calls.append("GET /balances/current")
        return dict(BALANCES)

    async def list_transfers(self, page_size=10, page_num=0):
        self.calls.append("GET /transfers")
        return list(self.items)

    async def list_transfer_page(self, page_size=10, page_num=0):
        self.calls.append("GET /transfers")
        start = page_num * page_size
        return list(self.items[start:start + page_size]), start + page_size < len(self.items)

    async def transfer(self, transfer_id):
        self.calls.append(f"GET /transfers/{transfer_id}")
        return next((t for t in self.items if t["id"] == transfer_id), transfer(transfer_id, "PROCESSING", "", ""))

    async def find_transfer_by_request_id(self, request_id):
        self.calls.append("GET /transfers?request_id")
        return self.existing

    async def validate_beneficiary(self, supplier):
        self.calls.append("POST /beneficiaries/validate")

    async def create_beneficiary(self, supplier, request_id):
        self.calls.append("POST /beneficiaries/create")
        return "ben_test"

    async def create_transfer(self, **kw):
        self.calls.append("POST /transfers/create")
        return TransferResult(transfer_id="tr_new", status="PROCESSING", raw={"id": "tr_new"})

    async def cancel_transfer(self, transfer_id):
        self.calls.append(f"POST /transfers/{transfer_id}/cancel")
        return {"status": "CANCELLED"}

    async def simulate_transfer_transition(self, transfer_id, next_status, failure_type=None):
        self.calls.append(f"POST /simulation/transfers/{transfer_id}/transition")

    def created(self) -> int:
        return self.calls.count("POST /transfers/create")


def reset(awx: FakeAwx | None = None) -> FakeAwx:
    awx = awx or FakeAwx()
    m.api.state.awx = awx
    m.PLANS.clear()
    m.PLAN_ORDER.clear()
    m.IN_FLIGHT.clear()
    m.ACCOUNT_LOCKED = False
    m.PIN_MISSES = 0
    m.CANCEL_BATCH = None
    return awx


def hold(plan) -> None:
    m._replace_plans([plan])


def approve(plan, pin="1234", fp=None):
    req = m.ApproveRequest(plan_id=plan.plan_id, fingerprint=fp or plan.fingerprint, pin=pin)
    try:
        return asyncio.run(m.approve(req)), None
    except HTTPException as exc:
        return None, exc.status_code


def test_guardrails():
    print("\nGuardrails")
    check("unknown supplier is rejected", rejected(supplier_query="Globex"))
    check("currency that doesn't match the supplier is rejected", rejected(currency="USD"))
    check("missing or blank invoice is rejected", rejected(reference="") and rejected(reference="   "))
    check("more than the wallet holds is rejected", rejected(amount=Decimal("999999")))
    check("zero or negative amount is rejected", rejected(amount=Decimal("0")) and rejected(amount=Decimal("-5")))
    swift = build_plan(intent(), BALANCES)
    local = build_plan(intent(supplier_query="Northwind", currency="USD", amount=Decimal("900")), BALANCES)
    check(
        "SWIFT fee is in the total debit, LOCAL is free",
        swift.total_debit == Decimal("4012.85") and local.fee == 0,
        f"{swift.total_debit} {local.fee}",
    )
    first = build_plan(intent(supplier_query="Brightline", currency="GBP", amount=Decimal("1")), BALANCES)
    check(
        "every payout needs approval at the default limit; first payment says so",
        all(p.decision is Decision.REQUIRES_APPROVAL for p in (swift, local, first))
        and any("First payment" in r for r in first.reasons),
    )
    again = build_plan(intent(), BALANCES)
    bigger = build_plan(intent(amount=Decimal("4001")), BALANCES)
    check(
        "same plan, same fingerprint; new amount, new fingerprint; fresh request_id each",
        swift.fingerprint == again.fingerprint and swift.fingerprint != bigger.fingerprint
        and swift.request_id != again.request_id,
    )


def test_pin_gate():
    print("\nApproval and PIN")
    awx = reset()
    plan = build_plan(intent(reference="INV-PIN-1"), BALANCES)
    hold(plan)
    _, code = approve(plan, pin="0000")
    check("a wrong PIN is refused and nothing is sent", code == 401 and awx.created() == 0, str(code))

    codes = []
    for n in range(2):
        fresh = build_plan(intent(reference=f"INV-PIN-{n + 2}"), BALANCES)
        hold(fresh)
        codes.append(approve(fresh, pin="0000")[1])
    check(
        "three misses lock the account, even across new drafts",
        codes == [401, 403] and m.ACCOUNT_LOCKED and not m.PLANS and awx.created() == 0,
        str(codes),
    )
    _, code = approve(plan)
    check("a locked account cannot approve", code == 403, str(code))

    reset(awx)
    m.PIN_MISSES = 2
    m._save_state()
    m.PIN_MISSES = 0
    m._load_state()
    check("PIN misses survive a server reload", m.PIN_MISSES == 2, str(m.PIN_MISSES))

    awx = reset()
    plan = build_plan(intent(reference="INV-PIN-OK"), BALANCES)
    hold(plan)
    _, code = approve(plan, fp="not-the-plan-on-screen")
    check("an approval for a different plan gets 409 and sends nothing", code == 409 and awx.created() == 0)
    body, code = approve(plan)
    check(
        "yes + correct PIN + matching fingerprint sends exactly once",
        code is None and awx.created() == 1 and body["transfer"]["transfer_id"] == "tr_new",
        f"{code} {awx.calls}",
    )

    def batch(plans, pin="1234", fps=None):
        items = [m.ApprovedItem(plan_id=p.plan_id, fingerprint=(fps or {}).get(p.plan_id, p.fingerprint)) for p in plans]
        try:
            return asyncio.run(m.approve_batch(m.BatchApproveRequest(items=items, pin=pin))), None
        except HTTPException as exc:
            return None, exc.status_code

    def queue():
        plans = [
            build_plan(intent(reference="INV-B1"), BALANCES),
            build_plan(intent(supplier_query="Northwind", currency="USD", amount=Decimal("900"), reference="INV-B2"), BALANCES),
        ]
        m._replace_plans(plans)
        return plans

    awx = reset()
    plans = queue()
    _, wrong = batch(plans, pin="0000")
    _, changed = batch(plans, fps={plans[1].plan_id: "edited"})
    check(
        "a batch with a wrong PIN, or any line changed, sends nothing",
        wrong == 401 and changed == 409 and awx.created() == 0 and len(m.PLANS) == 2,
        f"{wrong} {changed} {awx.calls}",
    )
    body, code = batch(plans)
    check(
        "one PIN sends every line of the batch, in order, once each",
        code is None and awx.created() == 2 and not m.PLANS and body["failed"] is None
        and [r["plan_id"] for r in body["results"]] == [p.plan_id for p in plans]
        and m.PIN_MISSES == 0,
        f"{code} {awx.calls}",
    )

    awx = reset(FakeAwx(items=[transfer("tr_sent", "PAID", "INV-B1", "2026-10-07T18:00:00+0000")]))
    plans = queue()
    body, code = batch(plans)
    check(
        "a batch stops at the first failure and leaves the rest waiting",
        code is None and awx.created() == 0 and not body["results"]
        and body["failed"]["plan_id"] == plans[0].plan_id and list(m.PLANS) == [plans[1].plan_id],
        f"{code} {body} {awx.calls}",
    )


def test_no_double_pay():
    print("\nNo double pay")
    plan = build_plan(intent(reference="INV-RETRY"), BALANCES)
    awx = reset(FakeAwx(existing=transfer("tr_old", "PROCESSING", "INV-RETRY", "2026-10-07T18:00:00+0000")))
    hold(plan)
    body, _ = approve(plan)
    check(
        "a retry adopts the transfer with the same request_id",
        body and body["duplicate_avoided"] and awx.created() == 0,
        str(awx.calls),
    )

    sent = transfer("tr_sent", "PAID", "INV-77", "2026-10-07T18:00:00+0000")
    awx = reset(FakeAwx(items=[sent]))
    plan = build_plan(intent(reference="INV-77"), BALANCES)
    hold(plan)
    _, code = approve(plan)
    check("an invoice already paid to this supplier is refused at approve", code == 409 and awx.created() == 0)

    awx = reset()
    plan = build_plan(intent(reference="INV-FLIGHT"), BALANCES)
    hold(plan)
    m.IN_FLIGHT.add(plan.plan_id)
    _, code = approve(plan)
    check("a second click while sending gets 409", code == 409 and awx.created() == 0)

    views = [m.view_transfer(t) for t in (sent, transfer("tr_x", "CANCELLED", "INV-88", "2026-10-07T18:00:00+0000"))]
    check(
        "the same invoice may pay another supplier, and a cancelled one may be reused",
        already_sent(views, "INV-77", "Northwind") is None and already_sent(views, "INV-88", "Acme") is None
        and already_sent(views, "inv 77", "Acme") is not None,
    )

    acme, northwind = (s for s in all_suppliers() if s.id in ("sup_acme_de", "sup_northwind_us"))
    history = [m.view_transfer(t) for t in (
        transfer("a1", "PAID", "INV-1", "2026-10-07T18:00:00+0000"),
        transfer("a2", "CANCELLED", "INV-2", "2026-10-07T18:00:00+0000"),
        transfer("a3", "PAID", "INV-03", "2026-10-07T18:00:00+0000"),
        transfer("a4", "PAID", "INV-1032", "2026-10-07T18:00:00+0000"),
        transfer("n1", "PAID", "INV-1", "2026-10-07T18:00:00+0000", payee="Northwind Freight Inc"),
    )]
    check(
        "the pay chip uses the smallest invoice that supplier never had, cancelled included",
        next_invoice(history, acme) == "INV-4" and next_invoice(history, northwind) == "INV-2"
        and next_invoice([], acme) == "INV-1",
        f"{next_invoice(history, acme)} {next_invoice(history, northwind)}",
    )


ITEMS = [
    transfer("aaaaaaaa-0001", "PAID", "INV-1", "2026-10-07T18:00:00+0000"),
    transfer("bbbbbbbb-0002", "PROCESSING", "INV-1032", "2026-10-07T18:05:00+0000"),
    transfer("cccccccc-0003", "PROCESSING", "INV-2000", "2026-10-07T18:10:00+0000"),
    transfer("dddddddd-0004", "PAID", "INV-3000", "2026-10-07T18:20:00+0000",
             payee="Northwind Freight Inc", currency="USD"),
]


def test_cancel_matching():
    print("\nCancel matching")
    chosen, question, _ = propose(ITEMS, [CancelQuery(reference="INV-1")])
    check("INV-1 does not match INV-1032", not chosen and "already went out" in (question or ""), question or "")

    chosen, question, _ = propose(ITEMS, [CancelQuery(scope="latest", limit=1)])
    check(
        "the latest is the newest by time; if it's paid, nothing older is picked",
        not chosen and "already went out" in (question or ""),
        str([c["reference"] for c in chosen]),
    )

    stale = [CancelQuery(reference="bbbbbbbb-0002")]
    queries = resolve_queries("cancel the last one", stale)
    chosen, _, _ = propose(ITEMS[:3], queries)
    check(
        "'the last one' ignores an old id the model copied",
        [c["id"] for c in chosen] == ["cccccccc-0003"],
        str([c["id"] for c in chosen]),
    )

    chosen, question, options = propose(ITEMS, [CancelQuery(supplier_query="Acme")])
    check("two open Acme transfers asks which, and picks none", not chosen and len(options) == 2 and question)

    reset(FakeAwx(items=ITEMS))
    picked = propose(ITEMS, [CancelQuery(reference="INV-2000")])[0]
    body = m._hold_cancel(m.ChatRequest(message="cancel INV-2000"), picked, "")
    req = m.CancelConfirm(batch_id=body["batch_id"], fingerprint="changed", confirm=True)
    try:
        asyncio.run(m.cancel_transfers(req))
        code = None
    except HTTPException as exc:
        code = exc.status_code
    awx = m.api.state.awx
    ok = asyncio.run(m.cancel_transfers(m.CancelConfirm(batch_id=body["batch_id"], fingerprint=body["fingerprint"])))
    try:
        asyncio.run(m.cancel_transfers(m.CancelConfirm(batch_id=body["batch_id"], fingerprint=body["fingerprint"])))
        again = None
    except HTTPException as exc:
        again = exc.status_code
    cancels = [c for c in awx.calls if c.endswith("/cancel")]
    check(
        "yes cancels exactly the shown transfer, once; a changed set gets 409",
        code == 409 and cancels == ["POST /transfers/cccccccc-0003/cancel"] and again == 404
        and ok["results"][0]["ok"],
        f"{code} {again} {cancels}",
    )


def tool_use(name, tool_id="t1", **inp):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=inp)],
    )


def text_reply(text):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def test_tools_never_move_money():
    print("\nTools")
    names = [t["name"] for t in TOOLS]
    check(
        "exactly the six read and draft tools exist",
        sorted(names) == sorted(TOOL_NAMES) and len(set(names)) == 6
        and not {"create_transfer", "cancel_transfer", "approve"} & set(names),
        str(names),
    )

    awx = reset(FakeAwx(items=[transfer("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "PROCESSING", "INV-1", "2026-10-07T18:00:00+0000")]))
    host = m.ToolSession("cancel the latest transfer")
    samples = {
        "get_balances": {},
        "list_suppliers": {},
        "list_transfers": {"scope": "latest", "limit": 1, "status": "PROCESSING"},
        "get_transfer": {"transfer_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        "draft_payout": {"supplier_query": "Acme", "amount": "100", "currency": "EUR", "reference": "INV-NEW"},
        "draft_cancel": {"scope": "latest", "limit": 1},
        "create_transfer": {"amount": "1"},
    }
    results = {name: asyncio.run(call_tool(name, args, host)) for name, args in samples.items()}
    money = [c for c in awx.calls if "/create" in c or c.endswith("/cancel") or "/simulation/" in c]
    check(
        "driving every tool, plus a made-up create_transfer, hits no money endpoint",
        money == [] and results["create_transfer"]["ok"] is False and host.plans and host.cancel_chosen,
        str(money or awx.calls),
    )

    bad = asyncio.run(call_tool("get_transfer", {"transfer_id": "../simulation/deposit/create"}, host))

    class Broken:
        async def get_balances(self):
            raise ValueError("boom")

    broken = asyncio.run(call_tool("get_balances", {}, Broken()))
    check(
        "a bad transfer id and a crashing tool both return an error, not a 500",
        bad["ok"] is False and broken["ok"] is False and "boom" in broken["error"],
    )

    reset()
    runaway = FakeModel([tool_use("get_balances", tool_id=str(i)) for i in range(MAX_TOOL_ROUNDS)])
    turn = asyncio.run(Copilot(client=runaway, model="test").run("hi", host=m.ToolSession()))
    host = m.ToolSession()
    host.plans.append(build_plan(intent(reference="INV-CAP"), BALANCES))
    out = asyncio.run(m._finish_turn(m.ChatRequest(message="x"), turn, host))
    check(
        "the tool loop stops at the cap and publishes no drafts",
        turn.stop_reason == "max_rounds" and len(runaway.calls) == MAX_TOOL_ROUNDS
        and out["type"] == "question" and not m.PLANS,
    )

    reset()
    host = m.ToolSession("pay acme and globex")
    asyncio.run(call_tool("draft_payout", samples["draft_payout"], host))
    asyncio.run(call_tool("draft_payout", {"supplier_query": "Globex", "amount": "5", "reference": "INV-9"}, host))
    out = asyncio.run(m._finish_turn(m.ChatRequest(message="x"), AgentTurn("Both drafted!", "end_turn"), host))
    check(
        "one blocked payout holds the whole message, in code's words",
        out["type"] == "blocked" and not m.PLANS and "Both drafted" not in out["question"],
        str(out),
    )

    reset(FakeAwx(items=[transfer("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "PAID", "INV-1", "2026-10-07T18:00:00+0000")]))
    host = m.ToolSession("show history")
    asyncio.run(call_tool("list_transfers", {"scope": "recent", "limit": 5}, host))
    req = m.ChatRequest(message="show history", session_id="memory-check")
    out = asyncio.run(m._finish_turn(req, AgentTurn("One payout, and it's paid.", "end_turn"), host))
    said = m.SESSIONS["memory-check"][-1]["content"]
    check(
        "a lookup card keeps Claude's short line as the lead, and history skips the rows",
        out["type"] == "list" and out["lead"] == "One payout, and it's paid." and out["rows"]
        and "aaaaaaaa" not in said and "INV-1" not in said,
        said,
    )

    day = [
        transfer(f"day-{i:03d}", "PAID", f"INV-{i}", "2026-10-07T19:00:00+0000")
        for i in range(101)
    ]
    day.append(transfer("other-day", "PAID", "INV-X", "2026-10-06T19:00:00+0000"))
    reset(FakeAwx(items=day))
    card = asyncio.run(m._transfer_history(m.TransferQuery(
        scope="range", since="2026-10-07", until="2026-10-07", limit=0,
    )))
    pages = [c for c in m.api.state.awx.calls if c == "GET /transfers"]
    check(
        "a date window returns every transfer that day, past the old cap of 8",
        isinstance(card, dict) and len(card["rows"]) == 101 and len(pages) == 2
        and all(row["id"] != "other-day" for row in card["rows"]),
        f"{len(card.get('rows', [])) if isinstance(card, dict) else card} pages={len(pages)}",
    )


def test_sandbox_timer():
    print("\nSandbox timer")
    check(
        "PROCESSING waits 45s for SENT (120s for Acme), then 15s for PAID",
        step_for("PROCESSING") == ("SENT", SENT_DELAY)
        and step_for("PROCESSING", "Northwind Freight Inc") == ("SENT", 45)
        and step_for("PROCESSING", "Acme Logistics GmbH") == ("SENT", ACME_PROCESSING_DELAY)
        and ACME_PROCESSING_DELAY == 120
        and step_for("SENT", "Acme Logistics GmbH") == ("PAID", PAID_DELAY)
        and (SENT_DELAY, PAID_DELAY) == (45, 15)
        and all(step_for(s) is None for s in ("SCHEDULED", "PAID", "CANCELLED")),
    )
    from app import sandbox_progress as sp

    async def started(base_url, status):
        awx = FakeAwx()
        awx.base_url = base_url
        m.api.state.awx = awx
        sp._tasks.pop("tr_t", None)
        m._resume_sandbox("tr_t", status)
        task = sp._tasks.pop("tr_t", None)
        if task:
            task.cancel()
        return task is not None

    check(
        "a status read resumes the timer in sandbox only, and only while it can move",
        asyncio.run(started("https://api.sandbox.airwallex.com", "PROCESSING"))
        and not asyncio.run(started("https://api.airwallex.com", "PROCESSING"))
        and not asyncio.run(started("https://api.sandbox.airwallex.com", "PAID")),
    )


ACME_TEXT = """ACME LOGISTICS GmbH
RECHNUNG / INVOICE
Acme Logistics GmbH · Friedrichstrasse 88 · 10117 Berlin
Payout Copilot Demo Co.
Rechnungsnr. / Invoice #
INV-demo-0
Datum / Date
01.09.2026
Kunden-Nr. / Customer #
K-20417
Nettobetrag / Net
8,197.13
Gesamtbetrag / Total EUR
8,197.13
Commerzbank AG · IBAN DE89 3704 0044 0532 0130 00 · BIC COBADEFFXXX"""

NORTHWIND_TEXT = """NORTHWIND
FREIGHT INC
INVOICE
BILL TO
INVOICE #
DATE
INV-demo-10
09/01/2026
Linehaul
1
1,464.14
1,464.14
BALANCE DUE
$6,338.70 USD
Northwind Freight Inc · Checking
Reference: INV-demo-10"""

BRIGHTLINE_TEXT = """invoice
Brightline Studio Ltd
14 Hoxton Square
Company no. 00000000 (demo)
BILLED TO
NUMBER
ISSUED
Payout Copilot Demo Co.
Accounts Payable
100 Market Street, Suite 400
San Francisco, CA 94105, USA
INV-demo-20
01 Sep 2026
Copywriting
£6,144.71
VAT (zero-rated export)
£0.00
Total due
£6,144.71 GBP"""


def tiny_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with plain text, built by hand so the test needs no files."""
    def esc(text):
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = "BT /F1 11 Tf 50 760 Td 14 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


def test_invoice_upload():
    print("\nInvoice upload")
    import base64

    from app.invoice_reader import parse_invoice, payout_message

    read = [parse_invoice(t) for t in (ACME_TEXT, NORTHWIND_TEXT, BRIGHTLINE_TEXT)]
    got = [(r["supplier"], r["amount"], r["currency"], r["invoice"]) for r in read]
    check(
        "the three invoice layouts give supplier, total, currency and invoice number",
        got == [
            ("Acme Logistics GmbH", "8197.13", "EUR", "INV-demo-0"),
            ("Northwind Freight Inc", "6338.70", "USD", "INV-demo-10"),
            ("Brightline Studio Ltd", "6144.71", "GBP", "INV-demo-20"),
        ] and all(not r["missing"] for r in read),
        str(got),
    )

    glued = parse_invoice(
        "Northwind Freight Inc\nInvoice number\nINV-demo-0Invoice date\n1 October 2026Due date\nTotal due 5,000.00 USD"
    )
    check(
        "an invoice number glued to the next label is still read",
        (glued["invoice"], glued["amount"], glued["currency"]) == ("INV-demo-0", "5000.00", "USD"),
        str(glued),
    )

    globex = parse_invoice("GLOBEX CORP\nInvoice # GX-77\nTotal due\n500.00 USD")
    no_total = parse_invoice("Acme Logistics GmbH\nInvoice # INV-9\nThanks")
    check(
        "an unknown supplier or a missing total is reported, not guessed",
        globex["missing"] == ["supplier"] and globex["supplier_seen"] == "GLOBEX CORP"
        and "amount" in no_total["missing"] and no_total["invoice"] == "INV-9",
        f"{globex} {no_total}",
    )

    ok = [{"ok": True, **r} for r in read[:2]]
    check(
        "readable invoices become one payout message, in upload order",
        payout_message(ok[:1]) == "Pay Acme Logistics GmbH 8197.13 EUR for invoice INV-demo-0"
        and payout_message(ok) == (
            "Pay these invoices: Acme Logistics GmbH 8197.13 EUR for invoice INV-demo-0; "
            "Northwind Freight Inc 6338.70 USD for invoice INV-demo-10"
        )
        and payout_message([{"ok": False}]) == "",
    )

    def upload(files):
        req = m.InvoiceUpload(files=[m.InvoiceFile(name=n, data=base64.b64encode(d).decode()) for n, d in files])
        try:
            return asyncio.run(m.read_invoices(req)), None
        except HTTPException as exc:
            return None, exc.status_code

    awx = reset()
    pdf = tiny_pdf(["Acme Logistics GmbH", "Invoice # INV-PDF-1", "Total due", "1,250.00 EUR"])
    body, code = upload([
        ("acme.pdf", pdf),
        ("again.pdf", pdf),
        ("notes.pdf", b"just text"),
    ])
    rows = body["invoices"] if body else []
    check(
        "a real PDF is read, a repeat is kept once, a non-PDF is refused, nothing is drafted or called",
        code is None and [r["ok"] for r in rows] == [True, False, False]
        and rows[0]["amount"] == "1250.00" and rows[0]["invoice"] == "INV-PDF-1"
        and "only kept it once" in rows[1]["error"] and "isn't a PDF" in rows[2]["error"]
        and body["message"] == "Pay Acme Logistics GmbH 1250.00 EUR for invoice INV-PDF-1"
        and not m.PLANS and not awx.calls,
        f"{code} {rows}",
    )
    _, too_many = upload([(f"{i}.pdf", pdf) for i in range(7)])
    _, none = upload([])
    check("more than 6 files, or none, is refused", too_many == 400 and none == 400, f"{too_many} {none}")

    six = FakeModel(
        [tool_use("draft_payout", tool_id=str(i)) for i in range(MAX_TOOL_ROUNDS + 1)]
        + [text_reply("All drafted.")]
    )
    turn = asyncio.run(Copilot(client=six, model="test").run("x", host=m.ToolSession(), max_rounds=MAX_TOOL_ROUNDS + 6))
    check(
        "an upload turn gets one extra tool round per invoice; typed turns keep the cap",
        turn.stop_reason == "end_turn" and MAX_TOOL_ROUNDS == 6,
        turn.stop_reason,
    )


if __name__ == "__main__":
    saved = {s.id: (s.previously_paid, s.beneficiary_id) for s in all_suppliers()}
    try:
        test_guardrails()
        test_pin_gate()
        test_no_double_pay()
        test_cancel_matching()
        test_tools_never_move_money()
        test_sandbox_timer()
        test_invoice_upload()
    finally:
        for s in all_suppliers():
            s.previously_paid, s.beneficiary_id = saved[s.id]
    print()
    if FAILS:
        raise SystemExit(f"{FAILS} core check(s) failed")
    print("core checks passed")
