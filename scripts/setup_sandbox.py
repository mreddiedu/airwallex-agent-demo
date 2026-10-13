#!/usr/bin/env python3
"""Sandbox dry run and funding.

Run this once before the first demo. It proves auth works, makes sure a Global
Account exists, funds it, and prints balances. It simulates a deposit on every
run.

    python scripts/setup_sandbox.py
    python scripts/setup_sandbox.py --amount 50000
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.airwallex import AirwallexClient, AirwallexError  # noqa: E402
from app.suppliers import all_suppliers  # noqa: E402

OK, BAD, INFO = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[36m·\033[0m"


async def main(amount: Decimal, currency: str, validate_suppliers: bool) -> int:
    load_dotenv()
    try:
        awx = AirwallexClient()
    except KeyError as exc:
        print(f"{BAD} Missing {exc} — copy .env.example to .env and fill it in.")
        return 1

    try:
        # 1. Auth
        token = await awx._bearer()
        print(f"{OK} Authenticated. Token {token[:18]}… expires {awx._expires_at:%H:%M:%S}")

        # 2. Global Accounts. Reuse one in the target currency before creating.
        accounts = await awx.global_accounts()
        print(f"{INFO} {len(accounts)} Global Account(s) on file")

        target = next(
            (a for a in accounts if currency in str(a.get("required_features", a))), None
        )
        if target is None and accounts:
            target = accounts[0]

        if target is None:
            print(f"{INFO} None found, creating a {currency} account…")
            created = await awx._call(
                "POST",
                "/global_accounts/create",
                json={
                    "request_id": str(uuid.uuid4()),
                    # country_code is the account's country, not its currency.
                    "country_code": "US" if currency == "USD" else "NL",
                    "required_features": [
                        {"currency": currency, "transfer_method": "LOCAL"}
                    ],
                },
            )
            target = created
            print(f"{OK} Created Global Account {created.get('id')}")

        account_id = target["id"]
        print(f"{OK} Using Global Account {account_id}")

        # 3. Fund it. Reports PENDING but posts immediately.
        await awx.simulate_deposit(account_id, amount, payer_name="Seed funding")
        print(f"{OK} Simulated deposit of {amount}")

        # 4. Balances
        balances = await awx.balances()
        if balances:
            for cur, amt in balances.items():
                print(f"{OK} Balance {cur} {amt:,}")
        else:
            print(f"{BAD} No balances returned. The deposit may not have landed.")

        # 5. Optional: confirm every supplier's bank details pass validation.
        if validate_suppliers:
            print()
            for supplier in all_suppliers():
                try:
                    await awx.validate_beneficiary(supplier)
                    print(f"{OK} {supplier.name} ({supplier.transfer_method}) validates")
                except AirwallexError as exc:
                    print(f"{BAD} {supplier.name}: {exc}")

        print(f"\n{OK} Sandbox is ready.")
        return 0

    except AirwallexError as exc:
        print(f"{BAD} {exc}")
        if exc.status in (401, 403):
            print("   Check AWX_CLIENT_ID / AWX_API_KEY and the key's scopes.")
        return 1
    finally:
        await awx.aclose()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--amount", type=Decimal, default=Decimal("25000"))
    p.add_argument("--currency", default="USD")
    p.add_argument("--skip-suppliers", action="store_true")
    args = p.parse_args()
    raise SystemExit(
        asyncio.run(main(args.amount, args.currency, not args.skip_suppliers))
    )
