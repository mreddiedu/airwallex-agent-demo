"""Match a cancel request to real transfers. Does not call Airwallex.

The model only says who, how much, and whether it meant the latest or all of
them. The ids come from the transfer list, and a vague match becomes a
question instead of a guess.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal

from .models import CancelQuery
from .suppliers import all_suppliers, resolve

_CANCEL_LEAD = re.compile(
    r"^\s*(please\s+)?((can|could) you\s+)?(cancel|stop|void|undo)\b",
    re.I,
)
_CANCEL_MENTION = re.compile(
    r"\b(cancel|stop|void|undo)\b.{0,48}\b(transfer|payout|payment|invoice|inv[- ]?\w+)",
    re.I,
)
_UUID = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.I,
)

# Sent or already finished. Cancel either will not work, or already happened.
_DONE = {"PAID", "SENT", "DISPATCHED"}
_STOPPED = {"CANCELLED", "FAILED", "REJECTED", "RECALLED"}
_CLOSED = _DONE | _STOPPED


def view_transfer(raw: dict) -> dict:
    """The fields a person needs in order to recognise a transfer."""
    amount = Decimal(str(raw.get("transfer_amount") or raw.get("amount") or "0"))
    currency = (raw.get("transfer_currency") or raw.get("currency") or "").upper()
    source = (raw.get("source_currency") or currency).upper()
    fee = Decimal(str(raw.get("fee_amount") or 0))
    fee_currency = (raw.get("fee_currency") or source).upper()
    payer = raw.get("amount_payer_pays")
    if payer is None:
        payer = amount + (fee if fee_currency == source else Decimal("0"))
    beneficiary = raw.get("beneficiary") or {}
    details = beneficiary.get("bank_details") or {} if isinstance(beneficiary, dict) else {}
    address = beneficiary.get("address") or {} if isinstance(beneficiary, dict) else {}
    payee = details.get("account_name") or "Unknown payee"
    return {
        "id": raw.get("id") or "",
        "status": (raw.get("status") or "").upper(),
        "amount": str(amount),
        "currency": currency,
        "source_currency": source,
        "fee": str(fee),
        "fee_currency": fee_currency,
        "payer_pays": str(Decimal(str(payer))),
        "reference": raw.get("reference") or "",
        "payee": payee,
        "country": _country_for(payee, details, address),
        "created_at": raw.get("created_at") or "",
        "short_reference_id": raw.get("short_reference_id") or "",
    }


def fingerprint(transfers: list[dict]) -> str:
    ids = "|".join(sorted(t["id"] for t in transfers))
    return hashlib.sha256(ids.encode()).hexdigest()[:16]


def looks_like_cancel(message: str) -> bool:
    text = message or ""
    return bool(_CANCEL_LEAD.search(text) or _CANCEL_MENTION.search(text))


def fallback_queries(message: str) -> list[CancelQuery] | None:
    """Used when the sentence clearly means cancel and there is no model parse."""
    if not looks_like_cancel(message or ""):
        return None
    low = message.lower()
    scope: str = "match"
    limit = 1
    if re.search(r"\b(all|every|everything|both)\b", low):
        scope = "all"
    count = re.search(r"\blast\s+(one|two|three|\d+)\b", low)
    if count or re.search(r"\b(latest|last|newest|recent)\b", low):
        scope = "latest" if scope != "all" else scope
        words = {"one": 1, "two": 2, "three": 3}
        if count:
            token = count.group(1)
            limit = words.get(token, int(token) if token.isdigit() else 1)
    status = ""
    if "processing" in low:
        status = "PROCESSING"
    elif "scheduled" in low:
        status = "SCHEDULED"
    currency = ""
    if "€" in message or re.search(r"\beur\b", low):
        currency = "EUR"
    elif "£" in message or re.search(r"\bgbp\b", low):
        currency = "GBP"
    elif "$" in message or re.search(r"\busd\b", low):
        currency = "USD"
    amount = None
    uuid_spans = [m.span() for m in _UUID.finditer(message)]
    for amount_match in re.finditer(r"(?:€|£|\$)?\s*(\d[\d,]*(?:\.\d+)?)", message):
        if count and count.start() <= amount_match.start() <= count.end():
            continue
        if any(start <= amount_match.start() < end for start, end in uuid_spans):
            continue
        window = low[max(0, amount_match.start() - 12): amount_match.end() + 8]
        if re.search(r"\b(inv|invoice|ref)\b", window):
            continue
        amount = Decimal(amount_match.group(1).replace(",", ""))
        break
    reference = ""
    ref_match = re.search(r"\b((?:inv|test)[-/]?[a-z0-9-]+)\b", low)
    uuid_match = _UUID.search(low)
    if ref_match:
        reference = ref_match.group(1).upper()
    elif uuid_match:
        reference = uuid_match.group(1)
    names = []
    for supplier in all_suppliers():
        tokens = [supplier.name.lower(), *supplier.aliases]
        if any(token and token in low for token in tokens):
            names.append(supplier.name)
    if scope == "match" and not (names or reference or status or currency or amount is not None):
        if re.search(r"\b(it|that|transfer|payout|payment)\b", low):
            scope = "latest"
        else:
            return None
    if not names:
        names = [""]
    scope_literal = scope if scope in ("match", "latest", "all") else "match"
    return [
        CancelQuery(
            supplier_query=name,
            amount=amount,
            currency=currency,
            reference=reference,
            status=status,
            scope=scope_literal,  # type: ignore[arg-type]
            limit=limit,
        )
        for name in names[:6]
    ]


def parse_created_at(raw: str) -> datetime | None:
    """Airwallex mixes `+0000`, `+00:00`, and fractional seconds. Strings do not sort."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def newest_first(item: dict) -> float:
    moment = parse_created_at(item.get("created_at") or "")
    return moment.timestamp() if moment else float("-inf")


def resolve_queries(message: str, model_queries: list[CancelQuery] | None) -> list[CancelQuery] | None:
    """Prefer the sentence when it plainly means the latest transfer.

    Chat history is full of older ids. The model will sometimes copy one of
    those into the cancel, which is a different transfer from the last one.
    """
    fallback = fallback_queries(message) if looks_like_cancel(message or "") else None
    if _plain_latest(fallback):
        return fallback
    if model_queries:
        return list(model_queries)
    return fallback


def _plain_latest(queries: list[CancelQuery] | None) -> bool:
    if not queries or len(queries) != 1:
        return False
    query = queries[0]
    if query.scope != "latest":
        return False
    return not (
        query.supplier_query
        or query.reference
        or query.currency
        or query.status
        or query.amount is not None
    )


def propose(raw_transfers: list[dict], queries: list[CancelQuery]) -> tuple[list[dict], str | None, list[dict]]:
    """Return transfers to cancel, a question, and matches to show when it is ambiguous."""
    views = [view_transfer(raw) for raw in raw_transfers if raw.get("id")]
    views.sort(key=newest_first, reverse=True)
    if not queries:
        return [], "Which transfer should I cancel?", []

    chosen: list[dict] = []
    seen: set[str] = set()
    for query in queries:
        hits, question, options = _select(views, query)
        if question:
            return [], question, options
        for hit in hits:
            if hit["id"] not in seen:
                seen.add(hit["id"])
                chosen.append(hit)
    if not chosen:
        return [], "I couldn't find an open transfer that matches that.", []
    if len(chosen) > 6:
        return [], "That's more than six. Name the payee or the invoice and I'll narrow it down.", []
    return chosen, None, []


def _select(views: list[dict], query: CancelQuery) -> tuple[list[dict], str | None, list[dict]]:
    filtered = [item for item in views if _matches(item, query)]
    open_items = [item for item in filtered if item["status"] not in _CLOSED]
    closed = [item for item in filtered if item["status"] in _CLOSED]

    if query.scope == "latest":
        # The last N by time, not the last N that are still open. Skipping a
        # paid or sent transfer and taking an older one cancels the wrong payout.
        window = filtered[: max(1, query.limit)]
        still_open = [item for item in window if item["status"] not in _CLOSED]
        closed_here = [item for item in window if item["status"] in _CLOSED]
        if still_open:
            return still_open, None, []
        return [], _closed_reason(query, closed_here) or "I don't see a transfer to cancel.", []

    if query.scope == "all":
        if not open_items:
            return [], _closed_reason(query, closed) or "Nothing open matches that.", []
        return open_items, None, []

    if not _has_filter(query):
        return [], "Which transfer should I cancel? A payee, an invoice, or the latest one.", []
    if len(open_items) == 1:
        return open_items, None, []
    if not open_items:
        return [], _closed_reason(query, closed) or f"I couldn't find an open transfer for {_label(query)}.", []
    shown = open_items[:8]
    extra = len(open_items) - len(shown)
    more = f", plus {extra} more" if extra else ""
    return [], f"A few transfers match {_label(query)}{more}. Which should I cancel?", shown


def _has_filter(query: CancelQuery) -> bool:
    return bool(query.supplier_query or query.reference or query.status or query.currency or query.amount is not None)


def _matches(item: dict, query: CancelQuery) -> bool:
    if query.supplier_query and not _payee_match(item["payee"], query.supplier_query):
        return False
    if query.amount is not None and Decimal(item["amount"]) != query.amount:
        return False
    if query.currency and item["currency"] != query.currency.upper():
        return False
    if query.status and item["status"] != query.status.upper():
        return False
    if query.reference and not reference_matches(item, query.reference):
        return False
    return True


def _country_for(payee: str, details: dict, address: dict) -> str:
    code = (
        details.get("bank_country_code")
        or details.get("country_code")
        or address.get("country_code")
        or ""
    )
    code = str(code).upper()
    if len(code) == 2:
        return code
    target = (payee or "").lower()
    for supplier in all_suppliers():
        names = [supplier.name, *supplier.aliases]
        if any(name.lower() == target or name.lower() in target for name in names if name):
            return supplier.country_code
    return ""


def _payee_match(payee: str, query: str) -> bool:
    supplier, candidates = resolve(query)
    target = payee.lower()
    if supplier is not None:
        names = [supplier.name, *supplier.aliases]
        return any(name.lower() in target or target in name.lower() for name in names)
    if len(candidates) > 1:
        return any(c.name.lower() in target for c in candidates)
    return query.strip().lower() in target


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def reference_matches(item: dict, reference: str) -> bool:
    """Whole invoice or reference only. INV-1 must not match INV-1032."""
    needle = _compact(reference)
    if not needle:
        return True
    invoice = _compact(item.get("reference") or "")
    if needle in (invoice, _compact(item.get("short_reference_id") or "")):
        return True
    if needle.isdigit() and invoice and re.sub(r"^[a-z]+", "", invoice) == needle:
        return True
    transfer_id = _compact(item.get("id") or "")
    return len(needle) >= 8 and transfer_id.startswith(needle)


def _label(query: CancelQuery) -> str:
    parts = [query.supplier_query, query.reference]
    if query.amount is not None:
        parts.append(f"{query.amount} {query.currency}".strip())
    elif query.currency:
        parts.append(query.currency)
    if query.status:
        parts.append(query.status)
    text = " ".join(part for part in parts if part).strip()
    return text or "that"


def _blurb(item: dict) -> str:
    ref = item["reference"] or "no invoice"
    return f"{item['payee']} {item['amount']} {item['currency']} ({ref}, {item['status']})"


def _who(query: CancelQuery) -> str:
    named = _label(query)
    if query.scope == "latest" and named == "that":
        return "latest" if query.limit == 1 else f"latest {query.limit}"
    return named


def _closed_reason(query: CancelQuery, closed: list[dict]) -> str | None:
    if not closed:
        return None
    item = closed[0]
    who = _who(query)
    if item["status"] in _STOPPED:
        return f"The {who} transfer is already {item['status'].lower()}."
    return (
        f"The {who} transfer already went out "
        f"({item['amount']} {item['currency']}, {item['status']}), so I can't cancel it."
    )
