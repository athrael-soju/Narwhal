"""Engine-specific routes and request fields around the OpenAI API."""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from .prefix import non_negative_ints


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
    keepalive_expiry_s: ClassVar[float] = 4.0
    engine_output_fields: ClassVar[tuple[str, ...]] = ()
    # Dotted client fields that would override Narwhal's engine controls.
    reserved_fields: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]: ...

    @abstractmethod
    def token_id_fields(self) -> dict[str, Any]: ...

    @abstractmethod
    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """Build the exact-token-count request."""

    @abstractmethod
    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        """Read a token count, or return None when the response has none."""

    @abstractmethod
    def tokenize_token_ids(self, payload: dict[str, Any]) -> list[int] | None:
        """Read the prompt token IDs, or return None when the response has none."""

    @abstractmethod
    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        """Return fields that force a probe to emit exactly `tokens`."""

    @abstractmethod
    def cold_probe_extras(self) -> dict[str, Any]:
        """Return fields that keep one probe from reusing any earlier cached prefix."""


class VllmDialect(EngineDialect):
    """Describe vLLM's serving API."""

    name = "vllm"
    # vLLM exposes this route only with VLLM_SERVER_DEV_MODE=1.
    cache_reset_path = "/reset_prefix_cache"
    prefill_incompatible = ("stream_options", "min_tokens", "n", "best_of", "max_completion_tokens")
    # vLLM streams per-chunk token ids for return_token_ids and honors stream_interval=1.
    token_ids = True
    # vLLM closes an idle connection after 5 s (VLLM_HTTP_TIMEOUT_KEEP_ALIVE).
    keepalive_expiry_s = 4.0
    engine_output_fields = ("token_ids", "prompt_token_ids")
    reserved_fields = (
        "vllm_xargs.kv_cache_report_mode",
        "vllm_xargs.kv_transfer_params",
        "vllm_xargs.ec_transfer_params",
    )
    # Chat template fields the tokenize route needs to count the engine's render.
    tokenize_passthrough: ClassVar[tuple[str, ...]] = (
        "tools",
        "chat_template",
        "chat_template_kwargs",
        "continue_final_message",
        "add_generation_prompt",
        "add_special_tokens",
    )

    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]:
        return {"x-request-id": rid}, {}

    def token_id_fields(self) -> dict[str, Any]:
        return {"return_token_ids": True, "stream_interval": 1}

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
            # The completion's own setting decides whether vLLM adds BOS and similar tokens.
            if "add_special_tokens" in body:
                payload["add_special_tokens"] = body["add_special_tokens"]
        return payload

    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        """Read vLLM's token count when present."""
        try:
            return int(payload["count"])
        except (KeyError, TypeError, ValueError):
            return None

    def tokenize_token_ids(self, payload: dict[str, Any]) -> list[int] | None:
        """Read vLLM's prompt token IDs when they agree with its count."""
        tokens = payload.get("tokens")
        if (
            not isinstance(tokens, list)
            or not non_negative_ints(tokens)
            or self.tokenize_response(payload) != len(tokens)
        ):
            return None
        return tokens

    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        """Force a decode probe to emit exactly `tokens` tokens."""
        return {"min_tokens": tokens, "ignore_eos": True}

    def cold_probe_extras(self) -> dict[str, Any]:
        """Salt the probe's first cache block with a fresh random value."""
        # vLLM hashes cache_salt into the first block; later block hashes chain from it.
        return {"cache_salt": secrets.token_urlsafe(32)}


# Each registered dialect has passed the fleet checks against its build.
_REGISTRY: dict[str, EngineDialect] = {d.name: d for d in (VllmDialect(),)}


def lookup(name: str) -> EngineDialect:
    """Return the registered dialect named by the fleet config."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown dialect {name!r}: known ones are {', '.join(sorted(_REGISTRY))}"
        ) from None
