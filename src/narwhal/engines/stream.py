from __future__ import annotations

import codecs
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

from .dialect import EngineDialect

# SSE ends lines with CRLF, CR or LF only.
_LINE_END = re.compile(r"\r\n|\r|\n")


@dataclass(slots=True)
class SseEvent:
    line: str
    data: dict[str, Any] | None = None
    done: bool = False
    malformed: bool = False


def parse_event(line: str) -> SseEvent:
    if not line.startswith("data:"):
        return SseEvent(line)
    payload = line[5:].strip()
    if payload == "[DONE]":
        return SseEvent(line, done=True)
    if not payload:
        return SseEvent(line)
    try:
        obj = json.loads(payload)
    except ValueError:
        return SseEvent(line, malformed=True)
    if not isinstance(obj, dict):
        return SseEvent(line, malformed=True)
    return SseEvent(line, obj)


class SseSplitter:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._partial = ""
        self._cr = False

    def feed(self, chunk: bytes) -> list[str]:
        return self._split(self._decoder.decode(chunk))

    def finish(self) -> list[str]:
        lines = self._split(self._decoder.decode(b"", final=True))
        if self._partial:
            lines.append(self._partial)
            self._partial = ""
        return lines

    def _split(self, text: str) -> list[str]:
        if not text:
            return []
        if self._cr and text.startswith("\n"):
            text = text[1:]
        self._cr = text.endswith("\r")
        lines = _LINE_END.split(self._partial + text)
        self._partial = lines.pop()
        return lines


async def sse_batches(chunks: AsyncIterator[bytes]) -> AsyncGenerator[list[SseEvent], None]:
    splitter = SseSplitter()
    async for chunk in chunks:
        batch = [parse_event(line) for line in splitter.feed(chunk) if line]
        if batch:
            yield batch
    tail = [parse_event(line) for line in splitter.finish() if line]
    if tail:
        yield tail


async def sse_events(chunks: AsyncIterator[bytes]) -> AsyncGenerator[SseEvent, None]:
    async for batch in sse_batches(chunks):
        for event in batch:
            yield event


def event_object(line: str) -> dict[str, Any] | None:
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
    choices = obj.get("choices", [])
    if not isinstance(choices, list) or any(not isinstance(choice, dict) for choice in choices):
        raise ValueError("SSE choices must be an array of objects")
    for choice in choices:
        delta = choice.get("delta")
        if delta is not None and not isinstance(delta, dict):
            raise ValueError("SSE delta must be an object")
    return choices


def token_ids(choices: list[dict[str, Any]]) -> tuple[int, ...] | None:
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


def _text_parts(event: SseEvent) -> list[str]:
    if event.data is None:
        return []
    parts = []
    for choice in event.data.get("choices", []) or []:
        text = choice.get("text")
        if text is None:
            text = (choice.get("delta") or {}).get("content")
        if text:
            parts.append(text)
    return parts


def sse_token_count(event: SseEvent) -> int:
    return len(_text_parts(event))


def sse_token_ids(event: SseEvent) -> tuple[int, ...] | None:
    if event.data is None:
        return ()
    try:
        return token_ids(event_choices(event.data))
    except ValueError:
        return None


def sse_token_bearing(event: SseEvent, dialect: EngineDialect) -> bool:
    if dialect.token_ids:
        ids = sse_token_ids(event)
        return ids is None or bool(ids)
    return sse_token_count(event) > 0


def rewrite_sse(event: SseEvent, *, strip: tuple[str, ...]) -> str:
    obj = event.data
    if obj is None:
        return event.line
    choices = [c for c in obj.get("choices", []) or [] if isinstance(c, dict)]
    for name in strip:
        obj.pop(name, None)
        for choice in choices:
            choice.pop(name, None)
    return "data: " + json.dumps(obj, separators=(",", ":"))


def sse_error(event: SseEvent) -> tuple[int, str] | None:
    obj = event.data
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
