"""One colored line per outbound API call, plus a redacted trace for the UI."""

from __future__ import annotations

import json
import logging
from collections import deque
from contextvars import ContextVar
from datetime import datetime

import httpx

log = logging.getLogger("payout.http")

_RESET = "\033[0m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"

_SECRET_NAMES = {
    "authorization",
    "x-api-key",
    "api-key",
    "api_key",
    "token",
    "access_token",
    "secret",
    "password",
    "cookie",
    "set-cookie",
}

_TRACE: deque[dict] = deque(maxlen=40)
_seq = 0
_active_tool: ContextVar[str] = ContextVar("active_tool", default="")


class using_tool:
    """Stamp outbound HTTP calls with the tool that caused them."""

    def __init__(self, name: str):
        self.name = name
        self._token = None

    def __enter__(self):
        self._token = _active_tool.set(self.name)
        return self

    def __exit__(self, *exc):
        _active_tool.reset(self._token)


def hooks() -> dict:
    return {"response": [_on_response]}


def since(after: int = 0) -> list[dict]:
    return [item for item in _TRACE if item["id"] > after]


def latest() -> int:
    """Ids restart at 1 when the server reloads. The page compares against this."""
    return _seq


def clear() -> None:
    _TRACE.clear()


async def _on_response(response: httpx.Response) -> None:
    request = response.request
    try:
        await response.aread()
    except Exception:
        pass
    status = response.status_code
    if status >= 400:
        status_color = _RED
    elif status >= 300:
        status_color = _YELLOW
    else:
        status_color = _GREEN
    call_color = _MAGENTA if "anthropic.com" in request.url.host else _CYAN
    hint = f" {_error_hint(response)}" if status >= 400 else ""
    log.info(
        "%s%s %s%s %s%s%s%s",
        call_color,
        request.method,
        request.url.path,
        _RESET,
        status_color,
        status,
        _RESET,
        hint,
    )
    try:
        _remember(response)
    except Exception:
        log.debug("trace skipped", exc_info=True)


def _remember(response: httpx.Response) -> None:
    global _seq
    request = response.request
    host = request.url.host
    path = request.url.path
    query = request.url.query
    if isinstance(query, bytes):
        query = query.decode("ascii", "replace")
    if query:
        path = f"{path}?{query}"
    _seq += 1
    body = _parse_body(response.content)
    tool = _active_tool.get()
    tools = _tool_names(body)
    _TRACE.append(
        {
            "id": _seq,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "service": "anthropic" if "anthropic.com" in host else "airwallex",
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "tool": tool,
            "tools": tools,
            "request": {
                "method": request.method,
                "url": str(request.url),
                "headers": _redact(dict(request.headers)),
                "body": _parse_body(request.content),
            },
            "response": {
                "status": response.status_code,
                "headers": _redact(dict(response.headers)),
                "body": body,
            },
        }
    )


def _tool_names(body) -> list[str]:
    """Tool names Claude asked for in this reply."""
    if not isinstance(body, dict):
        return []
    content = body.get("content")
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
            names.append(str(block["name"]))
    return names


def _parse_body(raw: bytes | None):
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return _redact(json.loads(text))
    except json.JSONDecodeError:
        return text[:4000]


def _mask(value) -> str:
    text = "" if value is None else str(value)
    tail = text[-4:] if len(text) > 4 else ""
    return f"....{tail}"


def _redact(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_NAMES:
                out[key] = _mask(item)
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _error_hint(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    code = body.get("code") or ""
    message = body.get("message") or body.get("error") or ""
    if isinstance(message, dict):
        message = message.get("message") or ""
    text = " ".join(part for part in (str(code), str(message)) if part).strip()
    return text[:120]
