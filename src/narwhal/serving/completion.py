"""Completion body validation and SSE response folding."""

from __future__ import annotations

import json
from typing import Any

_INTEGER_FIELDS = ("n", "best_of", "max_tokens")


def completion_body_error(body: Any) -> tuple[str, str | None] | None:
    """Validate router-interpreted fields and supported response formats.

    Return (message, param) for a rejected body, or None on success.
    Error messages identify the field and validation rule.
    """
    if not isinstance(body, dict):
        return "the request body must be a JSON object", None
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return "model must be a string", "model"
    stream = body.get("stream")
    if stream is not None and not isinstance(stream, bool):
        return "stream must be a boolean", "stream"
    for name in _INTEGER_FIELDS:
        value = body.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return f"{name} must be an integer", name
    prompt = body.get("prompt")
    if prompt is not None and not isinstance(prompt, str | list):
        return "prompt must be a string or an array", "prompt"
    messages = body.get("messages")
    if messages is not None:
        if not isinstance(messages, list):
            return "messages must be an array", "messages"
        if any(not isinstance(item, dict) for item in messages):
            return "every item in messages must be an object", "messages"
    if not stream:
        if body.get("audio") is not None:
            return "non-streaming audio output is not supported", "audio"
        if body.get("modalities") not in (None, ["text"]):
            return 'non-streaming modalities must be ["text"]', "modalities"
        tools = body.get("tools")
        if tools is not None:
            if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
                return "tools must be an array of objects", "tools"
            if any(tool.get("type", "function") != "function" for tool in tools):
                return "non-streaming tools must have type function", "tools"
    return None


def _fields(obj: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(key not in allowed and value is not None for key, value in obj.items()):
        raise ValueError(f"unsupported field in non-streaming {context}")


def _strings(target: dict[str, Any], delta: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        value = delta.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"non-streaming {field} must be a string")
            target[field] = (target.get(field) or "") + value


def _chat_delta(
    message: dict[str, Any], delta: dict[str, Any], tools: dict[int, dict[str, Any]]
) -> None:
    strings = ("content", "reasoning", "reasoning_content", "refusal")
    _fields(delta, {*strings, "role", "tool_calls", "function_call"}, "chat delta")
    if delta.get("role") not in (None, "assistant"):
        raise ValueError("non-streaming chat requires an assistant role")
    _strings(message, delta, strings)
    legacy = delta.get("function_call")
    if legacy is not None:
        if not isinstance(legacy, dict):
            raise ValueError("non-streaming function_call must be an object")
        _fields(legacy, {"name", "arguments"}, "function_call")
        _strings(
            message.setdefault("function_call", {"name": "", "arguments": ""}),
            legacy,
            ("name", "arguments"),
        )
    calls = delta.get("tool_calls")
    calls = [] if calls is None else calls
    if not isinstance(calls, list):
        raise ValueError("non-streaming tool_calls must be an array")
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError("non-streaming tool call must be an object")
        _fields(call, {"index", "id", "type", "function"}, "tool call")
        index = call.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("non-streaming tool call requires a nonnegative index")
        if call.get("type") not in (None, "function"):
            raise ValueError("non-streaming tool calls require type function")
        tool = tools.setdefault(
            index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        _strings(tool, call, ("id",))
        function = call.get("function")
        if function is not None:
            if not isinstance(function, dict):
                raise ValueError("non-streaming tool function must be an object")
            _fields(function, {"name", "arguments"}, "tool function")
            _strings(tool["function"], function, ("name", "arguments"))


def reassemble(lines: list[str], *, endpoint: str) -> dict[str, Any]:
    """Fold validated SSE into the response shape of the requested endpoint.

    Chunks reach this fold already filtered by the client's exposure choice,
    so token identity present in them was requested and survives the merge.
    """
    chat = endpoint == "/v1/chat/completions"
    merged: dict[str, Any] = {"index": 0, "finish_reason": "stop"}
    message: dict[str, Any] = {"role": "assistant", "content": None}
    tools: dict[int, dict[str, Any]] = {}
    text: list[str] = []
    token_ids: list[int] = []
    out: dict[str, Any] = {}
    for line in lines:
        raw = line.strip()
        if not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        obj = json.loads(payload)
        if not isinstance(obj, dict):
            raise ValueError("non-streaming SSE data must be an object")
        out.update(
            {k: v for k, v in obj.items() if k not in ("choices", "object") and v is not None}
        )
        choices = obj.get("choices")
        choices = [] if choices is None else choices
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("non-streaming response requires one output sequence")
        for choice in choices:
            if (
                not isinstance(choice, dict)
                or isinstance(choice.get("index"), bool)
                or choice.get("index", 0) != 0
            ):
                raise ValueError("non-streaming choice must have index zero")
            _fields(
                choice,
                {
                    "index",
                    "finish_reason",
                    "stop_reason",
                    "token_ids",
                    "prompt_token_ids",
                    "logprobs",
                    "delta" if chat else "text",
                },
                "choice",
            )
            for key in ("finish_reason", "stop_reason", "prompt_token_ids"):
                if choice.get(key) is not None:
                    merged[key] = choice[key]
            if chat:
                delta = choice.get("delta")
                delta = {} if delta is None else delta
                if not isinstance(delta, dict):
                    raise ValueError("non-streaming chat delta must be an object")
                _chat_delta(message, delta, tools)
            else:
                piece = choice.get("text")
                if piece is not None:
                    if not isinstance(piece, str):
                        raise ValueError("non-streaming completion text must be a string")
                    text.append(piece)
            logprobs = choice.get("logprobs")
            if logprobs is not None:
                if not isinstance(logprobs, dict):
                    raise ValueError("non-streaming logprobs must be an object")
                allowed = (
                    {"content", "refusal"}
                    if chat
                    else {"tokens", "token_logprobs", "top_logprobs", "text_offset"}
                )
                _fields(logprobs, allowed, "logprobs")
                accumulated = merged.setdefault("logprobs", {})
                for key, values in logprobs.items():
                    if values is None:
                        accumulated.setdefault(key, None)
                    elif isinstance(values, list):
                        if accumulated.get(key) is None:
                            accumulated[key] = []
                        accumulated[key].extend(values)
                    else:
                        raise ValueError("non-streaming logprobs fields must be arrays")
            raw_ids = choice.get("token_ids")
            if isinstance(raw_ids, list):
                token_ids.extend(
                    t for t in raw_ids if isinstance(t, int) and not isinstance(t, bool)
                )
    if tools:
        if any(not tool["id"] or not tool["function"]["name"] for tool in tools.values()):
            raise ValueError("non-streaming tool call lacks an ID or function name")
        message["tool_calls"] = [tools[index] for index in sorted(tools)]
    if "function_call" in message and not message["function_call"]["name"]:
        raise ValueError("non-streaming function_call lacks a name")
    merged["message" if chat else "text"] = message if chat else "".join(text)
    out["object"] = "chat.completion" if chat else "text_completion"
    if token_ids:
        merged["token_ids"] = token_ids
    out["choices"] = [merged]
    return out
