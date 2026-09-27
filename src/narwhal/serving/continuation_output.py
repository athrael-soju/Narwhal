"""Keep one outward completion identity while encoding qualified token events."""

from __future__ import annotations

import json
from typing import Any

from ..engines.replay import ReplayEvent
from .lifecycle import RequestLifecycle


def encode_event(state: RequestLifecycle, event: ReplayEvent, *, expose_ids: bool) -> bytes:
    """Serialise one event without leaking backend-attempt prompt or response metadata."""
    history = state.continuation
    if history is None or state.continuation_created is None:
        raise RuntimeError("continuation response has no history owner")
    if event.kind == "done":
        return b"data: [DONE]\n\n"
    envelope: dict[str, Any] = {
        "id": "cmpl-" + state.rid,
        "object": "text_completion",
        "created": state.continuation_created,
        "model": state.router.cfg.model,
        "choices": [],
    }
    if event.kind == "completion":
        choice: dict[str, Any] = {
            "index": 0,
            "text": event.text,
            "finish_reason": event.finish_reason,
        }
        source = event.envelope["choices"][0]
        if "stop_reason" in source:
            choice["stop_reason"] = source["stop_reason"]
        if expose_ids:
            choice["token_ids"] = event.generated_ids
            if not state.continuation_prompt_pending and history.committed_count == 0:
                choice["prompt_token_ids"] = history.replay_prompt()
                state.continuation_prompt_pending = True
        envelope["choices"] = [choice]
    if event.envelope.get("usage") is not None:
        count = history.observed_count + len(event.generated_ids)
        envelope["usage"] = {
            "prompt_tokens": history.prompt_count,
            "completion_tokens": count,
            "total_tokens": history.prompt_count + count,
        }
    return ("data: " + json.dumps(envelope, separators=(",", ":")) + "\n\n").encode()
