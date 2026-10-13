#!/usr/bin/env python3
"""Live, read-only checks against the real sandbox and Claude.

Run before a demo. Nothing here creates, sends, cancels, or deposits:
login, balances, one transfer read, beneficiary validation (which creates
nothing), and a one-token Claude call. The server runs this in the background
on every start; set PREFLIGHT=0 in .env to skip that.

    .venv/bin/python scripts/preflight.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
logging.disable(logging.CRITICAL)

from app.airwallex import AirwallexClient, AirwallexError  # noqa: E402
from app.suppliers import all_suppliers  # noqa: E402

# Six sample invoices run up to about 10,000 each.
LOW_BALANCE = Decimal("60000")
FAILS = 0


def report(ok: bool, name: str, detail: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f' — {detail}' if detail else ''}")
    if not ok:
        FAILS += 1


def warn(name: str, detail: str) -> None:
    print(f"  WARN  {name} — {detail}")


async def check_airwallex() -> None:
    awx = AirwallexClient()
    try:
        report("sandbox" in awx.base_url, "points at the sandbox", awx.base_url)
        try:
            await awx._bearer()
            report(True, "Airwallex login")
        except AirwallexError as exc:
            report(False, "Airwallex login", str(exc))
            return

        try:
            balances = await awx.balances()
            needed = sorted({s.currency for s in all_suppliers()})
            missing = [c for c in needed if balances.get(c, Decimal("0")) <= 0]
            shown = ", ".join(f"{c} {balances.get(c, Decimal('0')):,.2f}" for c in needed)
            report(not missing, "wallet can pay every supplier currency", shown)
            low = [c for c in needed if c not in missing and balances.get(c, Decimal("0")) < LOW_BALANCE]
            if low:
                warn("low balance", f"{', '.join(low)} under {LOW_BALANCE:,.0f}; a 6-invoice upload may be blocked")
        except AirwallexError as exc:
            report(False, "read balances", str(exc))

        try:
            await awx.list_transfer_page(page_size=1)
            report(True, "read transfer history")
        except AirwallexError as exc:
            report(False, "read transfer history", str(exc))

        for supplier in all_suppliers():
            try:
                await awx.validate_beneficiary(supplier)
                report(True, f"bank details validate: {supplier.name}", supplier.transfer_method)
            except AirwallexError as exc:
                report(False, f"bank details validate: {supplier.name}", str(exc))
    finally:
        await awx.aclose()


async def check_claude() -> None:
    from anthropic import APIError, AsyncAnthropic

    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        await client.messages.create(
            model=model, max_tokens=1, messages=[{"role": "user", "content": "ping"}]
        )
        report(True, "Claude answers", model)
    except APIError as exc:
        report(False, "Claude answers", f"{model}: {getattr(exc, 'message', exc)}")
    finally:
        await client.close()


async def main() -> int:
    print("Preflight (live, read-only)")
    absent = [k for k in ("AWX_CLIENT_ID", "AWX_API_KEY", "ANTHROPIC_API_KEY") if not os.getenv(k)]
    report(not absent, "keys present in .env", ", ".join(absent) and f"missing {', '.join(absent)}")
    if absent:
        return 1
    await check_airwallex()
    await check_claude()
    print("preflight passed" if not FAILS else f"preflight: {FAILS} check(s) failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
