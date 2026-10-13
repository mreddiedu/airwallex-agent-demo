"""Write nine simple sample invoices to samples/invoices/.

Three per supplier. Invoice numbers are INV-demo-0 through INV-demo-8.
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "invoices"

BILL_TO = (
    "Harbor Trading Co.",
    "200 Market Street",
    "San Francisco, CA 94105",
    "United States",
)

INVOICES = [
    {
        "number": "INV-demo-0",
        "supplier": "Northwind Freight Inc",
        "address": ("411 Alaskan Way", "Seattle, WA 98101", "United States"),
        "date": "1 October 2026",
        "due": "31 October 2026",
        "description": "Ocean freight, Seattle to Oakland",
        "amount": "5,000.00",
        "currency": "USD",
        "pay": ("Bank transfer", "Account 0071234567", "ABA 021000021"),
    },
    {
        "number": "INV-demo-1",
        "supplier": "Northwind Freight Inc",
        "address": ("411 Alaskan Way", "Seattle, WA 98101", "United States"),
        "date": "18 September 2026",
        "due": "18 October 2026",
        "description": "Warehouse handling, September",
        "amount": "1,750.00",
        "currency": "USD",
        "pay": ("Bank transfer", "Account 0071234567", "ABA 021000021"),
    },
    {
        "number": "INV-demo-2",
        "supplier": "Northwind Freight Inc",
        "address": ("411 Alaskan Way", "Seattle, WA 98101", "United States"),
        "date": "3 October 2026",
        "due": "2 November 2026",
        "description": "Inland delivery, three pallets",
        "amount": "9,200.00",
        "currency": "USD",
        "pay": ("Bank transfer", "Account 0071234567", "ABA 021000021"),
    },
    {
        "number": "INV-demo-3",
        "supplier": "Acme Logistics GmbH",
        "address": ("Friedrichstrasse 88", "10117 Berlin", "Germany"),
        "date": "28 September 2026",
        "due": "28 October 2026",
        "description": "Road freight, Berlin to Rotterdam",
        "amount": "4,000.00",
        "currency": "EUR",
        "pay": ("SWIFT transfer", "IBAN DE89 3704 0044 0532 0130 00", "SWIFT COBADEFFXXX"),
    },
    {
        "number": "INV-demo-4",
        "supplier": "Acme Logistics GmbH",
        "address": ("Friedrichstrasse 88", "10117 Berlin", "Germany"),
        "date": "12 September 2026",
        "due": "12 October 2026",
        "description": "Customs clearance, one shipment",
        "amount": "2,750.00",
        "currency": "EUR",
        "pay": ("SWIFT transfer", "IBAN DE89 3704 0044 0532 0130 00", "SWIFT COBADEFFXXX"),
    },
    {
        "number": "INV-demo-5",
        "supplier": "Acme Logistics GmbH",
        "address": ("Friedrichstrasse 88", "10117 Berlin", "Germany"),
        "date": "6 October 2026",
        "due": "5 November 2026",
        "description": "Warehousing, Berlin, October",
        "amount": "6,180.00",
        "currency": "EUR",
        "pay": ("SWIFT transfer", "IBAN DE89 3704 0044 0532 0130 00", "SWIFT COBADEFFXXX"),
    },
    {
        "number": "INV-demo-6",
        "supplier": "Brightline Studio Ltd",
        "address": ("14 Hoxton Square", "London N1 6NU", "United Kingdom"),
        "date": "1 October 2026",
        "due": "31 October 2026",
        "description": "Design retainer, October",
        "amount": "1,200.00",
        "currency": "GBP",
        "pay": ("Bank transfer", "Sort code 04-00-04", "Account 48362719"),
    },
    {
        "number": "INV-demo-7",
        "supplier": "Brightline Studio Ltd",
        "address": ("14 Hoxton Square", "London N1 6NU", "United Kingdom"),
        "date": "15 September 2026",
        "due": "15 October 2026",
        "description": "Brand guidelines, one round of revisions",
        "amount": "3,450.00",
        "currency": "GBP",
        "pay": ("Bank transfer", "Sort code 04-00-04", "Account 48362719"),
    },
    {
        "number": "INV-demo-8",
        "supplier": "Brightline Studio Ltd",
        "address": ("14 Hoxton Square", "London N1 6NU", "United Kingdom"),
        "date": "4 October 2026",
        "due": "3 November 2026",
        "description": "Packaging artwork",
        "amount": "860.00",
        "currency": "GBP",
        "pay": ("Bank transfer", "Sort code 04-00-04", "Account 48362719"),
    },
]


def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def invoice_pdf(inv: dict) -> bytes:
    """One A4-ish US Letter page. Helvetica only, so the file needs no fonts."""
    ops: list[str] = []

    def text(x, y, value, size=11, font="F1", gray=0):
        ops.append(f"{gray:.2f} g BT /{font} {size} Tf {x} {y} Td ({esc(value)}) Tj ET")

    def rule(x1, y, x2):
        ops.append(f"0.75 G {x1} {y} m {x2} {y} l S 0 G")

    text(54, 740, inv["supplier"], 18, "F2")
    for i, line in enumerate(inv["address"]):
        text(54, 716 - i * 14, line, 10, gray=0.35)

    text(430, 740, "INVOICE", 18, "F2")
    meta = (
        ("Invoice number", inv["number"]),
        ("Invoice date", inv["date"]),
        ("Due date", inv["due"]),
    )
    for i, (label, value) in enumerate(meta):
        y = 700 - i * 16
        text(430, y, label, 9, gray=0.35)
        text(430, y - 12, value, 11)

    rule(54, 660, 558)

    text(54, 636, "Bill to", 9, gray=0.35)
    for i, line in enumerate(BILL_TO):
        text(54, 618 - i * 14, line, 11, "F2" if i == 0 else "F1")

    rule(54, 540, 558)
    text(54, 522, "Description", 9, "F2", 0.35)
    text(430, 522, "Amount", 9, "F2", 0.35)
    rule(54, 512, 558)
    text(54, 490, inv["description"], 11)
    text(430, 490, f"{inv['amount']} {inv['currency']}", 11)
    rule(54, 472, 558)

    text(320, 446, "Total due", 11, "F2")
    text(430, 446, f"{inv['amount']} {inv['currency']}", 12, "F2")

    text(54, 390, "Please pay by", 9, gray=0.35)
    for i, line in enumerate(inv["pay"]):
        text(54, 372 - i * 14, line, 11, "F2" if i == 0 else "F1")

    text(54, 72, "Sample invoice for the payout demo. Not a demand for payment.", 9, gray=0.45)
    rule(54, 60, 558)

    stream = "\n".join(ops).encode()
    objects = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>"
    )
    add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for inv in INVOICES:
        path = OUT / f"{inv['number']}.pdf"
        path.write_bytes(invoice_pdf(inv))
        print(path.name, inv["supplier"], inv["amount"], inv["currency"])


if __name__ == "__main__":
    main()
