"""Decode SSE events and validate generated token identity."""

from __future__ import annotations

import json
from typing import Any

from .dialect import EngineDialect


def event_object(line: str) -> dict[str, Any] | None:
    """Decode a data object; return None for comments, empty data and terminators."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    obj = json.loads(payload)
    if not isinstance(obj, dict):
        raise ValueError("SSE data must be an object")
    return obj


def event_choices(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the event's choice array and validate each chat delta."""
    choices = obj.get("choices", [])
    if not isinstance(choices, list) or any(not isinstance(choice, dict) for choice in choices):
        raise ValueError("SSE choices must be an array of objects")
    for choice in choices:
        delta = choice.get("delta")
        if delta is not None and not isinstance(delta, dict):
            raise ValueError("SSE delta must be an object")
    return choices


def token_ids(choices: list[dict[str, Any]]) -> tuple[int, ...] | None:
    """Collect nonnegative integer IDs; return None for invalid or unidentified output."""
    tokens: list[int] = []
    for choice in choices:
        delta = choice.get("delta") or {}
        output = choice.get("text") or any(
            delta.get(field)
            for field in (
                "content",
                "reasoning",
                "reasoning_content",
                "tool_calls",
                "function_call",
                "refusal",
            )
        )
        raw = choice.get("token_ids")
        if raw is None:
            if output:
                return None
            continue
        if not isinstance(raw, list) or not all(
            isinstance(token, int) and not isinstance(token, bool) and token >= 0 for token in raw
        ):
            return None
        if output and not raw:
            return None
        tokens.extend(raw)
    return tuple(tokens)


def _text_parts(line: str) -> list[str]:
    obj = _sse_object(line)
    if obj is None:
        return []
    parts = []
    for choice in obj.get("choices", []) or []:
        text = choice.get("text")
        if text is None:
            text = (choice.get("delta") or {}).get("content")
        if text:
            parts.append(text)
    return parts


def sse_text(line: str) -> str:
    """Extract completion text and chat content from one SSE line."""
    return "".join(_text_parts(line))


def sse_token_count(line: str) -> int:
    """Count choices carrying completion text or chat content."""
    return len(_text_parts(line))


def sse_token_ids(line: str) -> tuple[int, ...] | None:
    """Read event token IDs; return None for invalid identity or choice structure."""
    obj = _sse_object(line)
    if obj is None:
        return ()
    try:
        return token_ids(event_choices(obj))
    except ValueError:
        return None


def sse_token_bearing(line: str, dialect: EngineDialect) -> bool:
    """Detect generated output for the first-token deadline."""
    if dialect.token_ids:
        ids = sse_token_ids(line)
        return ids is None or bool(ids)
    return sse_token_count(line) > 0


def rewrite_sse(
    line: str,
    *,
    expose_token_ids: bool,
) -> str:
    """Hide internal token fields unless the client requested them."""
    obj = _sse_object(line)
    if obj is None:
        return line
    if not expose_token_ids:
        obj.pop("prompt_token_ids", None)
        obj.pop("token_ids", None)
        for choice in obj.get("choices", []) or []:
            if isinstance(choice, dict):
                choice.pop("prompt_token_ids", None)
                choice.pop("token_ids", None)
    return "data: " + json.dumps(obj, separators=(",", ":"))


def _sse_object(line: str) -> dict[str, Any] | None:
    """Read a data object for helpers that skip malformed frames."""
    try:
        return event_object(line)
    except ValueError:
        return None


def sse_error(line: str) -> tuple[int, str] | None:
    """Return an upstream error carried inside an HTTP 200 SSE stream."""
    obj = _sse_object(line)
    if obj is None or obj.get("error") is None:
        return None
    error = obj["error"]
    status = 500
    detail = "engine returned an SSE error"
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
            status = code
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            detail = message.strip()
        elif error:
            detail = json.dumps(error, sort_keys=True, separators=(",", ":"))
    elif isinstance(error, str) and error.strip():
        detail = error.strip()
    else:
        detail = json.dumps(error, sort_keys=True, separators=(",", ":"))
    return status, detail
