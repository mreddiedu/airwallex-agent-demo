"""Sandbox-only transfer timeline.

Airwallex moves a new payout to PROCESSING on its own. After that, this waits
45 seconds (120 for Acme Logistics GmbH) and asks the sandbox to mark it SENT,
then waits 15 seconds and asks for PAID. Production never calls the simulation
endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .airwallex import AirwallexClient, AirwallexError
from .suppliers import resolve

log = logging.getLogger("payout.sandbox")

SENT_DELAY = 45
ACME_PROCESSING_DELAY = 120
PAID_DELAY = 15
_TERMINAL = {"PAID", "CANCELLED", "FAILED"}
_tasks: dict[str, asyncio.Task] = {}
_clock: dict[str, dict] = {}


def step_for(status: str, payee: str = "") -> tuple[str, int] | None:
    """The next sandbox step once a transfer is already in `status`."""
    if status == "PROCESSING":
        return ("SENT", _processing_delay(payee))
    if status == "SENT":
        return ("PAID", PAID_DELAY)
    return None


def _processing_delay(payee: str) -> int:
    supplier, _candidates = resolve(payee)
    if supplier is not None and supplier.id == "sup_acme_de":
        return ACME_PROCESSING_DELAY
    return SENT_DELAY


def _payee(body: dict) -> str:
    beneficiary = body.get("beneficiary") or {}
    details = beneficiary.get("bank_details") if isinstance(beneficiary, dict) else {}
    return (details or {}).get("account_name") or ""


def countdown(transfer_id: str) -> dict | None:
    item = _clock.get(transfer_id)
    if not item:
        return None
    return {
        "next_status": item.get("next_status") or "",
        "due_at": item.get("due_at") or "",
        "error": item.get("error") or "",
    }


def start(awx: AirwallexClient, transfer_id: str) -> None:
    """Schedule the PROCESSING → SENT → PAID timeline. Safe to call twice."""
    if not transfer_id or "sandbox" not in (awx.base_url or ""):
        return
    existing = _tasks.get(transfer_id)
    if existing and not existing.done():
        return
    if (_clock.get(transfer_id) or {}).get("error"):
        return
    _tasks[transfer_id] = asyncio.get_running_loop().create_task(_drive(awx, transfer_id))


def _arm(transfer_id: str, next_status: str, delay: int) -> None:
    due = datetime.now(timezone.utc) + timedelta(seconds=delay)
    _clock[transfer_id] = {"next_status": next_status, "due_at": due.isoformat(), "error": ""}


async def _snapshot(awx: AirwallexClient, transfer_id: str) -> tuple[str, str]:
    body = await awx.transfer(transfer_id)
    return (body.get("status") or "").upper(), _payee(body)


async def _until_ready(awx: AirwallexClient, transfer_id: str) -> tuple[str, str]:
    """Wait until the sandbox has reached a status we can advance, or a final one."""
    status, payee = "", ""
    for _ in range(20):
        status, seen = await _snapshot(awx, transfer_id)
        payee = payee or seen
        if status in _TERMINAL or step_for(status):
            return status, payee
        await asyncio.sleep(2)
    return status, payee


async def _advance(awx: AirwallexClient, transfer_id: str, current: str, payee: str) -> tuple[str, str]:
    step = step_for(current, payee)
    if step is None:
        return current, payee
    nxt, delay = step
    _arm(transfer_id, nxt, delay)
    log.info("sandbox %s %s -> %s in %ss", transfer_id, current, nxt, delay)
    await asyncio.sleep(delay)
    status, seen = await _snapshot(awx, transfer_id)
    payee = payee or seen
    if status != current:
        log.info("sandbox %s left %s for %s before the timer", transfer_id, current, status)
        return status, payee
    await awx.simulate_transfer_transition(transfer_id, nxt)
    log.info("sandbox %s now %s", transfer_id, nxt)
    return nxt, payee


async def _drive(awx: AirwallexClient, transfer_id: str) -> None:
    try:
        status, payee = await _until_ready(awx, transfer_id)
        if status == "PROCESSING":
            status, payee = await _advance(awx, transfer_id, "PROCESSING", payee)
        if status == "SENT":
            await _advance(awx, transfer_id, "SENT", payee)
        elif status and status not in _TERMINAL:
            log.info("sandbox %s stopped at %s", transfer_id, status)
    except AirwallexError as exc:
        log.warning("sandbox %s: %s", transfer_id, exc)
        _clock[transfer_id] = {
            "next_status": "",
            "due_at": "",
            "error": "The sandbox couldn't move this transfer.",
        }
        return
    except Exception:
        log.exception("sandbox %s", transfer_id)
        _clock[transfer_id] = {
            "next_status": "",
            "due_at": "",
            "error": "The sandbox couldn't move this transfer.",
        }
        return
    _clock.pop(transfer_id, None)
