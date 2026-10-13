"""Vetted supplier directory.

This exists so the model never produces bank details. It resolves a name the
user typed to an entry here; if nothing matches confidently, the agent asks
instead of guessing. In a real deployment this is the supplier master record.

Bank details are synthetic account numbers behind real routing identifiers,
because Airwallex validates routing codes, sort codes and IBAN checksums at
beneficiary creation. Invented routing codes fail with a bare code 010.
"""

from __future__ import annotations

from .models import Supplier

_DIRECTORY: list[Supplier] = [
    Supplier(
        id="sup_acme_de",
        name="Acme Logistics GmbH",
        aliases=["acme", "acme logistics", "acme berlin", "acme gmbh"],
        country_code="DE",
        currency="EUR",
        # DE/EUR uses SWIFT: IBAN plus swift_code, no routing fields.
        transfer_method="SWIFT",
        bank_details={
            "account_name": "Acme Logistics GmbH",
            "account_currency": "EUR",
            "bank_country_code": "DE",
            "iban": "DE89370400440532013000",
            "swift_code": "COBADEFFXXX",
        },
        address={
            "street_address": "Friedrichstrasse 88",
            "city": "Berlin",
            "postcode": "10117",
            "country_code": "DE",
        },
        previously_paid=True,
    ),
    Supplier(
        id="sup_northwind_us",
        name="Northwind Freight Inc",
        aliases=["northwind", "northwind freight", "northwind us"],
        country_code="US",
        currency="USD",
        transfer_method="LOCAL",
        bank_details={
            "account_name": "Northwind Freight Inc",
            "account_currency": "USD",
            "bank_country_code": "US",
            "account_number": "0071234567",
            "account_routing_type1": "aba",
            "account_routing_value1": "021000021",
            # Airwallex rejects any other casing with code 016.
            "bank_account_category": "Checking",
        },
        address={
            "street_address": "411 Alaskan Way",
            "city": "Seattle",
            "state": "WA",
            "postcode": "98101",
            "country_code": "US",
        },
        previously_paid=True,
    ),
    Supplier(
        id="sup_brightline_gb",
        name="Brightline Studio Ltd",
        aliases=["brightline", "brightline studio", "brightline london"],
        country_code="GB",
        currency="GBP",
        transfer_method="LOCAL",
        bank_details={
            "account_name": "Brightline Studio Ltd",
            "account_currency": "GBP",
            "bank_country_code": "GB",
            # GB/GBP requires bank_name, which the US corridor does not.
            "bank_name": "Monzo Bank Limited",
            "account_number": "48362719",
            # Lowercase only. Airwallex rejects SORT_CODE with code 011.
            "account_routing_type1": "sort_code",
            "account_routing_value1": "040004",
        },
        address={
            "street_address": "14 Hoxton Square",
            "city": "London",
            "postcode": "N1 6NU",
            "country_code": "GB",
        },
        # Never paid before, so a payout to this supplier always needs approval.
        previously_paid=False,
    ),
]


def all_suppliers() -> list[Supplier]:
    return _DIRECTORY


def by_id(supplier_id: str) -> Supplier | None:
    return next((s for s in _DIRECTORY if s.id == supplier_id), None)


def resolve(query: str) -> tuple[Supplier | None, list[Supplier]]:
    """Match a user-typed name to the directory.

    Returns (match, candidates). A match is only returned when exactly one
    entry matches, so "pay the studio" with two studios on file comes back
    ambiguous rather than picking one.
    """
    q = query.strip().lower()
    if not q:
        return None, []

    exact = [s for s in _DIRECTORY if s.name.lower() == q or q in s.aliases]
    if len(exact) == 1:
        return exact[0], exact

    partial = [
        s
        for s in _DIRECTORY
        if q in s.name.lower()
        or s.name.lower().startswith(q)
        or any(q in a or a in q for a in s.aliases)
    ]
    if len(partial) == 1:
        return partial[0], partial
    return None, partial


def mark_paid(supplier_id: str, beneficiary_id: str | None = None) -> None:
    supplier = by_id(supplier_id)
    if supplier:
        supplier.previously_paid = True
        if beneficiary_id:
            supplier.beneficiary_id = beneficiary_id
