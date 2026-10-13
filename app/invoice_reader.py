"""Read the four payout fields from an uploaded PDF invoice.

Only the supplier, the amount, the currency and the invoice number are taken.
Bank details on the invoice are ignored: they always come from the supplier
directory. A field that cannot be read is reported, never guessed.
"""

from __future__ import annotations

import io
import re
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

from .suppliers import all_suppliers

MAX_FILES = 6
MAX_BYTES = 5 * 1024 * 1024

_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY"}
_CODES = {"EUR", "USD", "GBP", "CNY", "JPY", "AUD", "CAD", "HKD", "SGD", "NZD", "CHF"}
# Strongest wording first. "Net" and "subtotal" are never the amount to pay.
_TOTAL_LABELS = (
    ("balance due", "amount due", "total due", "amount payable"),
    ("gesamtbetrag", "grand total", "invoice total"),
    ("total",),
)
_NOT_TOTAL = ("subtotal", "sub-total", "net", "nettobetrag", "vat", "tax")
_INVOICE_LABEL = re.compile(
    r"(invoice\s*(#|no\.?|number|nr\.?)|rechnungsnr\.?|rechnungsnummer|^number$|^no\.?$)",
    re.I,
)
_MONEY = re.compile(r"(?<![\w.,])[€$£¥]?\s?\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{2})?(?![\w])")
_DATE = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")


class InvoiceFileError(ValueError):
    """The upload is not something we can read."""


def pdf_text(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise InvoiceFileError("is larger than 5 MB")
    if not data.startswith(b"%PDF"):
        raise InvoiceFileError("isn't a PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise InvoiceFileError("is password protected")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except InvoiceFileError:
        raise
    except Exception as exc:  # pypdf raises many types on a damaged file
        raise InvoiceFileError("couldn't be opened") from exc
    if not text.strip():
        raise InvoiceFileError("has no readable text (a scan needs OCR)")
    return text


def _lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def _supplier(text: str) -> tuple[str, str]:
    """(directory name, raw name). A name not on file comes back as raw only."""
    low = text.lower()
    found = []
    for supplier in all_suppliers():
        names = [supplier.name.lower(), *supplier.aliases]
        if any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", low) for name in names):
            found.append(supplier)
    if len(found) == 1:
        return found[0].name, found[0].name
    first = next((line for line in _lines(text) if len(line) > 2 and not line.lower().startswith("invoice")), "")
    return "", first


def _invoice_token(line: str) -> str:
    text = line.strip().strip(":#").strip()
    # Text extraction can glue the next label on: "INV-demo-0Invoice date".
    glued = re.match(r"^([A-Za-z0-9][\w/-]*?\d)(?=[A-Z][a-z])", text)
    token = glued.group(1) if glued else text
    if not token or " " in token or len(token) > 40 or _DATE.match(token):
        return ""
    if not re.search(r"\d", token):
        return ""
    if re.search(r"[A-Za-z]", token):
        return token
    # Digits only, like 2024-0042. Never an amount such as 1,074.74.
    return token if len(token) >= 4 and re.fullmatch(r"[\d/-]+", token) else ""


def _invoice_number(lines: list[str]) -> str:
    for i, line in enumerate(lines):
        match = _INVOICE_LABEL.search(line)
        if not match:
            continue
        for word in line[match.end():].split():
            same = _invoice_token(word)
            if same:
                return same
        for nxt in lines[i + 1:i + 8]:
            token = _invoice_token(nxt)
            if token:
                return token
    for line in lines:
        ref = re.search(r"\breference:?\s*([A-Za-z][\w-]*\d[\w-]*)", line, re.I)
        if ref:
            return ref.group(1)
        named = re.search(r"\buse\s+([A-Za-z][\w-]*\d[\w-]*)\s+as\s+(?:your\s+)?reference", line, re.I)
        if named:
            return named.group(1)
    return ""


def _number(raw: str) -> Decimal | None:
    digits = re.sub(r"[^\d.,]", "", raw)
    if not digits:
        return None
    if "," in digits and "." in digits:
        decimal_mark = "," if digits.rfind(",") > digits.rfind(".") else "."
    elif "," in digits and len(digits.split(",")[-1]) == 2:
        decimal_mark = ","
    else:
        decimal_mark = "."
    thousands = "." if decimal_mark == "," else ","
    clean = digits.replace(thousands, "").replace(decimal_mark, ".")
    try:
        value = Decimal(clean)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _currency_in(text: str) -> str:
    for code in re.findall(r"\b[A-Z]{3}\b", text):
        if code in _CODES:
            return code
    for symbol, code in _SYMBOLS.items():
        if symbol in text:
            return code
    return ""


def _total(lines: list[str]) -> tuple[Decimal | None, str]:
    for labels in _TOTAL_LABELS:
        for i in range(len(lines) - 1, -1, -1):
            low = lines[i].lower()
            if not any(label in low for label in labels):
                continue
            if any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low) for word in _NOT_TOTAL):
                if not any(label in low for label in _TOTAL_LABELS[0] + _TOTAL_LABELS[1]):
                    continue
            for candidate in (lines[i], *lines[i + 1:i + 3]):
                money = _MONEY.findall(candidate)
                if not money:
                    continue
                amount = _number(money[-1])
                if amount is None:
                    continue
                currency = _currency_in(candidate) or _currency_in(lines[i])
                return amount, currency
    return None, ""


def parse_invoice(text: str) -> dict:
    """The four fields, plus `missing` for any that could not be read."""
    lines = _lines(text)
    supplier, raw_name = _supplier(text)
    amount, currency = _total(lines)
    if amount is not None and not currency:
        found = {code for code in re.findall(r"\b[A-Z]{3}\b", text) if code in _CODES}
        currency = found.pop() if len(found) == 1 else ""
    invoice = _invoice_number(lines)
    missing = []
    if not supplier:
        missing.append("supplier")
    if amount is None:
        missing.append("amount")
    if not currency:
        missing.append("currency")
    if not invoice:
        missing.append("invoice number")
    return {
        "supplier": supplier,
        "supplier_seen": raw_name,
        "amount": f"{amount:.2f}" if amount is not None else "",
        "currency": currency,
        "invoice": invoice,
        "missing": missing,
    }


def read_invoice(name: str, data: bytes) -> dict:
    try:
        fields = parse_invoice(pdf_text(data))
    except InvoiceFileError as exc:
        return {"file": name, "ok": False, "error": f"{name} {exc}.", "missing": []}
    if fields["missing"]:
        what = ", ".join(fields["missing"])
        if fields["missing"] == ["supplier"] and fields["supplier_seen"]:
            error = f"{name} is from {fields['supplier_seen']}, who isn't a supplier on file."
        else:
            error = f"I couldn't read the {what} on {name}."
        return {"file": name, "ok": False, "error": error, **fields}
    return {"file": name, "ok": True, **fields}


def payout_message(invoices: list[dict]) -> str:
    """One chat message for every readable invoice, in upload order."""
    parts = [
        f"{inv['supplier']} {inv['amount']} {inv['currency']} for invoice {inv['invoice']}"
        for inv in invoices
        if inv.get("ok")
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return f"Pay {parts[0]}"
    return "Pay these invoices: " + "; ".join(parts)
