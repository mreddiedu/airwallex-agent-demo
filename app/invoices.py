"""One invoice can go out once per supplier.

The same number can pay a different supplier. A cancelled or failed transfer
did not pay the invoice, so that supplier can use the number again. Anything
else already sent, and the newest of those is the record to show.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .cancel import parse_created_at
from .models import Supplier
from .suppliers import resolve

_NOT_SENT = {"CANCELLED", "FAILED", "REJECTED", "RECALLED"}


def invoice_key(reference: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (reference or "").casefold())


def already_sent(views: list[dict], reference: str, supplier_query: str) -> dict | None:
    """The newest transfer this supplier already sent for this invoice."""
    key = invoice_key(reference)
    supplier, _candidates = resolve(supplier_query)
    if not key or supplier is None:
        return None
    hits = [
        view
        for view in views
        if invoice_key(view.get("reference") or "") == key
        and str(view.get("status") or "").upper() not in _NOT_SENT
        and _same_supplier(view.get("payee") or "", supplier)
    ]
    if not hits:
        return None
    hits.sort(key=_newest, reverse=True)
    return hits[0]


_NUMBERED = re.compile(r"^inv-?0*(\d+)$", re.I)


def next_invoice(views: list[dict], supplier: Supplier) -> str:
    """Smallest INV-n this supplier has never had on any transfer, cancelled included."""
    used = set()
    for view in views:
        if not _same_supplier(view.get("payee") or "", supplier):
            continue
        match = _NUMBERED.match((view.get("reference") or "").strip())
        if match:
            used.add(int(match.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"INV-{n}"


def _same_supplier(payee: str, supplier: Supplier) -> bool:
    target = payee.casefold().strip()
    if not target:
        return False
    names = [supplier.name, *supplier.aliases]
    return any(
        name.casefold() == target or name.casefold() in target or target in name.casefold()
        for name in names
        if name
    )


def _newest(view: dict) -> datetime:
    moment = parse_created_at(view.get("created_at") or "")
    return moment or datetime.min.replace(tzinfo=timezone.utc)
