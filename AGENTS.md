# Instructions for AI coding agents

Payout Copilot is a FastAPI app. Claude drafts supplier payouts against the real Airwallex sandbox, and a person approves each one with yes plus a PIN. Read [README.md](README.md) first. It is the source of truth for setup.

## Set up

1. Python 3.9 or newer. `python3 -m venv .venv`, then `.venv/bin/pip install -r requirements.txt`.
2. `cp .env.example .env`. Ask the user for `AWX_CLIENT_ID`, `AWX_API_KEY`, and `ANTHROPIC_API_KEY`. Never invent, print, log, or commit them.
3. `.venv/bin/python scripts/test_core.py` must end with `core checks passed`. It needs no keys or network.
4. With keys in place, `.venv/bin/python scripts/preflight.py` must end with `preflight passed`. It is read-only.
5. Start: `.venv/bin/python -m uvicorn app.main:app --reload --port 8000`. If login fails with `proxy_blocked`, your shell injects a proxy; run it outside the sandbox or ask the user to start it.

## Never, unless the user asks in this conversation

- Approve a payout, enter a PIN, or say yes on a receipt or cancel card.
- Cancel a transfer.
- Run `scripts/setup_sandbox.py`. It simulates a deposit every run.
- Point `AWX_BASE_URL` at production.
- Commit `.env` or `state.json`.

## Rules when changing code

- Money moves only in `/api/approve`, `/api/approve-batch`, and `/api/cancel-transfers`. Never give a tool in `app/tools.py` a path to `transfers/create`, cancel, or simulation.
- Payout decisions (fee, rail, limits, blocks) belong in `app/policy.py`, not the prompt.
- Bank details come only from `app/suppliers.py`.
- Amounts are major units (`100` is one hundred). Use `Decimal`.
- Keep Python 3.9 syntax: `Optional[...]`, not `X | None`, in runtime-evaluated annotations.
- Add a check to `scripts/test_core.py` for any behavior change, and run it. The server refuses to start if it fails.
- Airwallex conventions that cause silent bugs are in `.cursor/rules/airwallex-api.mdc`.
