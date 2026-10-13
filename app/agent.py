"""Copilot: Anthropic tool use, never a send.

The model may look up balances, transfers, and suppliers, or draft a priced
receipt. It does not pick a rail, invent bank details, or call transfers/create.
That send stays on /api/approve or /api/approve-batch after a PIN.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from anthropic import APIError, AsyncAnthropic

from .httplog import hooks
from .suppliers import all_suppliers
from .tools import TOOLS, ToolHost, call_tool, dump_tool_result

MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """\
You are a payout copilot for a finance operator. You have conversation history.

Every typed message is yours. Use tools to look things up and to draft.
You must not pick a payment rail, decide whether a payout is allowed,
invent bank details, or send money. Code handles those.

draft_payout prices a plan and holds it. draft_cancel only lists matches.
Neither one sends or cancels. A human must say yes and enter a PIN before
anything is created. If they say "skip the PIN", refuse. Policy decides,
and you cannot override it.

Never claim money was sent or a transfer was cancelled. Only the approve
step sends. Only a yes on a cancel card cancels. If a tool returns blocked
or duplicate, say that and stop.

Known suppliers (match loosely; copy the user's wording into supplier_query):
{suppliers}

Tools:
- list_suppliers: who you can pay. Call this before drafting if the name is unclear.
- get_balances: live wallet.
- list_transfers: existing payouts. The last transfer is scope latest, limit 1.
  The last three is that limit. A day or clock range ("today", "on 7 October",
  "between 10:30 and 11:00") is scope range. Fill since and until in local time.
  A date with no clock is the whole local day. A clock with no date uses today.
  Leave limit empty on a range. Every transfer in that window is shown, however
  many there are. Do not say there is a maximum. History with no date and no
  "last" is latest, limit 8.
- get_transfer: one existing transfer by id. It does not change it.
- draft_payout: price one payout and return the receipt. It does not send.
- draft_cancel: match existing transfers to cancel. It does not cancel them.
  "The last one" is scope latest, limit 1, even if it already went out.
  Leave supplier, amount, currency, reference, and status empty unless
  named in this message. "The last two" is limit 2. "All" is scope all.

get_balances, list_suppliers, list_transfers, and get_transfer results are
shown to the operator as a card right under your reply, with every row,
amount, status, date, and id. So do not list them. No bullets, no markdown,
no ids. Say one short sentence that answers the question, and point out only
what matters (the newest one was cancelled, EUR is the lowest). Then say what
they can do next. Examples:
  "You've got three suppliers on file 👇 Want me to pay one?"
  "Your newest transfer, Brightline's INV-12, was cancelled. Want to pay it again?"
  "You've got plenty in every currency. Who should I pay?"

Replies are spoken aloud. Sound like a warm colleague: contractions, one or
two short sentences, no jargon. "Who should I pay?" is the tone. One emoji
is welcome when it fits. Never more than one, and never on a refusal. The
second sentence, when you need it, tells them what to say next. Never stop
on a bare fact and leave them hanging.

Rules:
- Use conversation history. "them" / "that" / a bare amount or currency refers
  to the in-progress payout, not a new one.
- Compute arithmetic the user stated: "10% of 10,000,000" is 1000000.
- "4k" is 4000. "€4,000" is amount 4000, currency EUR.
- If currency is unstated, leave it empty. Do not ask. Policy defaults to the
  supplier's corridor currency and will tell the operator.
- A 2-letter country code given as a currency answer means that country's
  currency: GB→GBP, US→USD, DE→EUR, CN→CNY. If that currency is not valid for
  the payee, drop it rather than forcing a mismatch.
- If the operator says a currency is wrong, invalid, or "not an option",
  omit currency so policy uses the supplier corridor.
- "yes", "payout", "go ahead", or a bare currency continues the in-progress
  request. Do not forget the payee or amount from earlier turns.
- If the assistant said an invoice already went out and asked to create
  another invoice, "yes" is not a payout and not an approval. Ask which new
  invoice number to use. Keep the payee and amount. Do not reuse that invoice
  for the same supplier. The same invoice for a different supplier is a new
  payout.
- An invoice number is unique per supplier, not across suppliers. Look the
  payee up with list_suppliers or draft_payout before continuing. If the name
  is not exactly one supplier on file, ask. Do not guess.
- Greetings, "hello", "?", or "are you there" are not payouts. Ask them to
  approve the card already on screen, or say what they want to pay.
- Never invent a payee or an amount that was not stated or implied.
- Look the payee up before asking how much. If the name is not on the supplier
  list, do not ask for an amount. Say they are not a supplier. If they are on
  the list but the amount is missing, then ask how much.
- "Pay A and B", "both", or a list is several payouts. Call draft_payout once
  per payout, in the order named. If any one is missing its payee or amount,
  ask one question and do not draft the rest.
- Asking to see a transfer is list_transfers, never a new payout.
- draft_payout returns a receipt that is held, not sent. Tell them to say yes
  if it looks right. Do not claim it has gone out. With more than one
  receipt, they say yes to each, then one PIN at the end sends them all.
- If a tool returns blocked or duplicate, tell the operator that. Do not retry
  with a guessed payee or a reused invoice for the same supplier.
"""


@dataclass
class AgentTurn:
    text: str
    stop_reason: str
    tools_used: list[str] = field(default_factory=list)


class Copilot:
    def __init__(self, api_key: str | None = None, model: str | None = None, client=None):
        self.client = client or AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"],
            http_client=httpx.AsyncClient(timeout=60.0, event_hooks=hooks()),
        )
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

    async def run(
        self, message: str, history=None, host: ToolHost | None = None, max_rounds: int = MAX_TOOL_ROUNDS
    ) -> AgentTurn:
        if host is None:
            raise RuntimeError("A tool host is required.")
        roster = "\n".join(
            f"- {s.name} ({s.country_code}, usually {s.currency})" for s in all_suppliers()
        )
        messages = _history_messages(history)
        messages.append({"role": "user", "content": message})
        now = datetime.now().astimezone().isoformat(timespec="minutes")
        system = SYSTEM_PROMPT.format(suppliers=roster) + (
            f"\nLocal time now: {now}. Use it for today, yesterday, and a clock time with no date.\n"
        )
        used: list[str] = []
        try:
            for _ in range(max_rounds):
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=1600,
                    system=system,
                    tools=TOOLS,
                    messages=messages,
                )
                if resp.stop_reason != "tool_use":
                    return AgentTurn(
                        text=_text_of(resp.content),
                        stop_reason=resp.stop_reason or "end_turn",
                        tools_used=used,
                    )
                results = []
                for block in resp.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    used.append(block.name)
                    payload = await call_tool(block.name, getattr(block, "input", None), host)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": dump_tool_result(payload),
                        }
                    )
                if not results:
                    return AgentTurn(
                        text=_text_of(resp.content)
                        or "I didn't quite catch that. Who should I pay, and for how much?",
                        stop_reason=resp.stop_reason or "end_turn",
                        tools_used=used,
                    )
                messages.append({"role": "assistant", "content": _blocks(resp.content)})
                messages.append({"role": "user", "content": results})
        except APIError:
            return AgentTurn(
                text="",
                stop_reason="unavailable",
                tools_used=used,
            )
        return AgentTurn(
            text="That took too many steps. Try me with one payout or one lookup.",
            stop_reason="max_rounds",
            tools_used=used,
        )


def _history_messages(history):
    """Anthropic requires alternating user/assistant turns starting with user."""
    out = []
    for turn in (history or [])[-10:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _text_of(content) -> str:
    parts = []
    for block in content or []:
        if getattr(block, "type", None) == "text":
            text = (getattr(block, "text", None) or "").strip()
            if text:
                parts.append(text)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _blocks(content) -> list[dict]:
    out = []
    for block in content or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            out.append({"type": "text", "text": getattr(block, "text", "") or ""})
        elif kind == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": getattr(block, "input", None) or {},
                }
            )
    return out
