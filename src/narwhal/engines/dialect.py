"""Engine-specific routes and request fields around the OpenAI API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class EngineDialect(ABC):
    """Describe the HTTP details that differ between engine builds."""

    name: ClassVar[str]
    health_path: ClassVar[str] = "/health"
    # None selects character-ratio estimates in callers.
    tokenize_path: ClassVar[str | None] = "/tokenize"
    # None means the engine exposes no HTTP cache-reset capability.
    cache_reset_path: ClassVar[str | None] = None
    # These client fields conflict with the forced one-token prefill request.
    prefill_incompatible: ClassVar[tuple[str, ...]] = ()
    # True when streamed decode honors return_token_ids and stream_interval.
    token_ids: ClassVar[bool] = False

    @abstractmethod
    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """Build the exact-token-count request."""

    @abstractmethod
    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        """Read a token count, or return None when the response has none."""

    @abstractmethod
    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        """Return fields that force a probe to emit exactly `tokens`."""


class VllmDialect(EngineDialect):
    """Describe vLLM's serving API."""

    name = "vllm"
    # vLLM exposes this route only with VLLM_SERVER_DEV_MODE=1.
    cache_reset_path = "/reset_prefix_cache"
    prefill_incompatible = ("stream_options", "min_tokens", "n", "best_of")
    # vLLM answers return_token_ids with per-chunk token ids and honors
    # stream_interval=1, so streamed output is exactly countable.
    token_ids = True
    # Chat fields that steer the server-side template render, forwarded to
    # the tokenize route so the count covers the render the engine makes.
    tokenize_passthrough: ClassVar[tuple[str, ...]] = (
        "tools",
        "chat_template",
        "chat_template_kwargs",
        "continue_final_message",
        "add_generation_prompt",
        "add_special_tokens",
    )

    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """Build vLLM's tokenization request from an OpenAI request body."""
        payload: dict[str, Any] = {"model": model}
        if "messages" in body:
            payload["messages"] = body["messages"]
            for field_name in self.tokenize_passthrough:
                if field_name in body:
                    payload[field_name] = body[field_name]
        else:
            payload["prompt"] = body.get("prompt", "")
        return payload

    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        """Read vLLM's token count when present."""
        try:
            return int(payload["count"])
        except (KeyError, TypeError, ValueError):
            return None

    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        """Force a decode probe to emit exactly `tokens` tokens."""
        return {"min_tokens": tokens, "ignore_eos": True}


# Register a dialect only after the fleet checks pass against that build.
_REGISTRY: dict[str, EngineDialect] = {d.name: d for d in (VllmDialect(),)}


def lookup(name: str) -> EngineDialect:
    """Return the registered dialect named by the fleet config."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown dialect {name!r}: known ones are {', '.join(sorted(_REGISTRY))}"
        ) from None
