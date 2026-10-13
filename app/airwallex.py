"""Airwallex sandbox REST client.

Every sandbox-only call (deposit simulation, transfer transition) is isolated in
the `Simulation` section so the decision logic above it does not change when
this is pointed at production.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from .httplog import hooks
from .models import Supplier, TransferResult

# Flat fee Airwallex charges per SWIFT payout in sandbox. LOCAL payouts are free.
SWIFT_FEE = Decimal("12.85")
SWIFT_FEE_CURRENCY = "EUR"


class AirwallexError(RuntimeError):
    """Carries the Airwallex error code so callers can branch on it.

    The same account state surfaces under different codes per endpoint:
    transfers return `balance_insufficient`, charges `insufficient_fund`,
    FX `insufficient_funds`.
    """

    def __init__(self, status: int, code: str | None, message: str, body: dict):
        super().__init__(f"[{status}] {code or 'unknown'}: {message}")
        self.status = status
        self.code = code
        self.body = body


class AirwallexClient:
    def __init__(
        self,
        client_id: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.client_id = client_id or os.environ["AWX_CLIENT_ID"]
        self.api_key = api_key or os.environ["AWX_API_KEY"]
        self.base_url = (
            base_url or os.getenv("AWX_BASE_URL", "https://api.sandbox.airwallex.com")
        ).rstrip("/")
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._login_lock: asyncio.Lock | None = None
        self._http = httpx.AsyncClient(timeout=30.0, event_hooks=hooks())

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- auth -----------------------------------------------------------

    async def _bearer(self) -> str:
        """Return a cached token, refreshing a minute before it expires.

        Airwallex tokens last 30 minutes and are meant to be reused; calling
        login per request gets you rate limited.
        """
        if self._fresh():
            return self._token
        # Created here so it binds to the running loop on Python 3.9.
        if self._login_lock is None:
            self._login_lock = asyncio.Lock()
        async with self._login_lock:
            if self._fresh():
                return self._token
            return await self._login()

    def _fresh(self) -> bool:
        now = datetime.now(timezone.utc)
        return bool(self._token and self._expires_at and now < self._expires_at)

    async def _login(self) -> str:
        now = datetime.now(timezone.utc)
        resp = await self._send(
            "POST",
            f"{self.base_url}/api/v1/authentication/login",
            headers={"x-client-id": self.client_id, "x-api-key": self.api_key},
        )
        body = _json(resp)
        if resp.status_code not in (200, 201):
            raise AirwallexError(
                resp.status_code, body.get("code"), body.get("message", "login failed"), body
            )

        self._token = body["token"]
        expires_at = body.get("expires_at")
        self._expires_at = (
            _parse_ts(expires_at) - timedelta(seconds=60)
            if expires_at
            else now + timedelta(minutes=25)
        )
        return self._token

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Call Airwallex, turning a sandbox proxy block into a normal error.

        Cursor's default shell sets HTTP_PROXY. That proxy answers 403 for
        Airwallex, and httpx then raises ProxyError before any response exists.
        """
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.ProxyError as exc:
            raise AirwallexError(
                403,
                "proxy_blocked",
                "Local HTTP proxy blocked Airwallex (403). Restart the server with unrestricted network.",
                {},
            ) from exc
        except httpx.TransportError as exc:
            raise AirwallexError(
                0,
                "connect_error",
                f"Could not reach Airwallex: {exc}",
                {},
            ) from exc

    async def _call(self, method: str, path: str, json: dict | None = None, **params) -> dict:
        token = await self._bearer()
        resp = await self._send(
            method,
            f"{self.base_url}/api/v1{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json,
            params=params or None,
        )
        body = _json(resp)
        if resp.status_code >= 400:
            details = body.get("details") or {}
            errors = details.get("errors") or body.get("errors")
            extra = f" {errors}" if errors else ""
            raise AirwallexError(
                resp.status_code,
                body.get("code"),
                (body.get("message") or body.get("source") or "request failed") + extra,
                body,
            )
        return body

    # --- reads ----------------------------------------------------------

    async def balances(self) -> dict[str, Decimal]:
        rows = await self._call("GET", "/balances/current")
        if isinstance(rows, dict):
            rows = rows.get("items", [])
        return {r["currency"]: Decimal(str(r.get("available_amount", 0))) for r in rows}

    async def global_accounts(self) -> list[dict]:
        body = await self._call("GET", "/global_accounts")
        return body.get("items", body) if isinstance(body, dict) else body

    async def transfer(self, transfer_id: str) -> dict:
        return await self._call("GET", f"/transfers/{transfer_id}")

    async def find_transfer_by_request_id(self, request_id: str) -> dict | None:
        """Used before any retry. If the first attempt actually landed, this
        finds it instead of creating a duplicate payment.
        """
        body = await self._call("GET", "/transfers", request_id=request_id)
        items = body.get("items", []) if isinstance(body, dict) else body
        return items[0] if items else None

    async def list_transfers(self, page_size: int = 10, page_num: int = 0) -> list:
        items, _has_more = await self.list_transfer_page(page_size=page_size, page_num=page_num)
        return items

    async def list_transfer_page(self, page_size: int = 10, page_num: int = 0) -> tuple[list, bool]:
        body = await self._call("GET", "/transfers", page_size=page_size, page_num=page_num)
        items = body.get("items", []) if isinstance(body, dict) else body
        has_more = bool(body.get("has_more")) if isinstance(body, dict) else False
        return items or [], has_more

    # --- beneficiaries --------------------------------------------------

    async def validate_beneficiary(self, supplier: Supplier) -> dict:
        """Validates bank details without creating anything.

        Airwallex validates routing codes and IBAN checksums here rather than at
        payment time, so this is where bad details surface. The payload key is
        `transfer_methods` — sending `payment_methods` returns a code 001 that
        names a field you did not send.
        """
        return await self._call(
            "POST",
            "/beneficiaries/validate",
            json={
                "beneficiary": _beneficiary_payload(supplier),
                "transfer_methods": [supplier.transfer_method],
            },
        )

    async def create_beneficiary(self, supplier: Supplier, request_id: str) -> str:
        body = await self._call(
            "POST",
            "/beneficiaries/create",
            json={
                "request_id": request_id,
                "beneficiary": _beneficiary_payload(supplier),
                "transfer_methods": [supplier.transfer_method],
            },
        )
        # The id is returned as `id` at the top level; the `beneficiary` key
        # holds the echoed payload, not an identifier.
        beneficiary_id = body.get("id") or body.get("beneficiary_id")
        if not beneficiary_id:
            raise AirwallexError(
                200, "unexpected_response", f"No beneficiary id in response: {list(body)}", body
            )
        return beneficiary_id

    # --- money movement -------------------------------------------------

    async def create_transfer(
        self,
        *,
        request_id: str,
        beneficiary_id: str,
        amount: Decimal,
        currency: str,
        reference: str,
        transfer_method: str,
    ) -> TransferResult:
        """Create a payout.

        `request_id` must be the one minted with the plan. Airwallex rejects a
        reused id for a *different* operation with `duplicate_request_id`, which
        is exactly the protection we want on a retry.

        Field names are `transfer_currency` / `transfer_amount`; sending
        `payment_currency` returns code 001.
        """
        body = await self._call(
            "POST",
            "/transfers/create",
            json={
                "request_id": request_id,
                "beneficiary_id": beneficiary_id,
                "transfer_currency": currency,
                "transfer_amount": str(amount),
                "source_currency": currency,
                "transfer_method": transfer_method,
                "reason": "professional_business_services",
                "reference": reference,
            },
        )
        return _transfer_result(body)

    async def cancel_transfer(self, transfer_id: str) -> dict:
        """Stop a transfer that has not been sent on.

        Airwallex returns `update_unsupported` once the status can no longer
        be cancelled. The body is empty; the id is the only input.
        """
        return await self._call("POST", f"/transfers/{transfer_id}/cancel", json={})

    # --- simulation (sandbox only) --------------------------------------

    async def simulate_deposit(
        self, global_account_id: str, amount: Decimal, payer_name: str = "Seed funding"
    ) -> dict:
        """Funds a Global Account. Reports PENDING but posts immediately."""
        return await self._call(
            "POST",
            "/simulation/deposit/create",
            json={
                "global_account_id": global_account_id,
                "amount": str(amount),
                "payer_name": payer_name,
            },
        )

    async def simulate_transfer_transition(
        self, transfer_id: str, next_status: str, failure_type: str | None = None
    ) -> dict:
        """Advance a transfer to a terminal state.

        A transfer must reach SENT before a failure_type applies. Prefer a
        FAILED transition with an explicit failure_type over a bare cancel,
        which only reports `failure_reason: "simulation"`.
        """
        payload: dict = {"next_status": next_status}
        if failure_type:
            payload["failure_type"] = failure_type
        return await self._call(
            "POST", f"/simulation/transfers/{transfer_id}/transition", json=payload
        )


# --- helpers ------------------------------------------------------------


def _json(resp: httpx.Response) -> dict:
    """Normalise a response body to a dict.

    Not every failure comes back as Airwallex JSON — a WAF or proxy in front of
    the API can return an HTML error page. Collapse those to a short message
    rather than letting markup leak into an exception string.
    """
    try:
        body = resp.json()
    except ValueError:
        text = " ".join(resp.text.split())
        if text.lower().startswith("<!doctype") or text.startswith("<"):
            text = f"non-JSON response from {resp.url.host} (HTTP {resp.status_code})"
        return {"message": text[:200] or f"HTTP {resp.status_code}"}
    return body if isinstance(body, dict) else {"items": body}


def _parse_ts(value: str) -> datetime:
    # Airwallex returns ISO8601 with a +0000 offset.
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.now(timezone.utc) + timedelta(minutes=25)


def _beneficiary_payload(supplier: Supplier) -> dict:
    return {
        "entity_type": "COMPANY",
        "company_name": supplier.name,
        "bank_details": supplier.bank_details,
        "address": supplier.address,
    }


def _transfer_result(body: dict) -> TransferResult:
    return TransferResult(
        transfer_id=body.get("id") or body.get("transfer_id"),
        status=body.get("status", "UNKNOWN"),
        failure_type=body.get("failure_type"),
        failure_reason=body.get("failure_reason"),
        raw=body,
    )


def fee_for(transfer_method: str) -> tuple[Decimal, str]:
    """SWIFT payouts cost a flat fee from the same wallet. LOCAL is free."""
    if transfer_method == "SWIFT":
        return SWIFT_FEE, SWIFT_FEE_CURRENCY
    return Decimal("0"), ""
