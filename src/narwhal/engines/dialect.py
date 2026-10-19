from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from .prefix import non_negative_ints


class EngineDialect(ABC):
    name: ClassVar[str]
    health_path: ClassVar[str] = "/health"
    # None selects character-ratio estimates in callers.
    tokenize_path: ClassVar[str | None] = "/tokenize"
    # None reads the context limit from the tokenize response.
    context_limit_path: ClassVar[str | None] = None
    # None means the engine exposes no HTTP cache-reset capability.
    cache_reset_path: ClassVar[str | None] = None
    # These client fields conflict with the forced one-token prefill request.
    prefill_incompatible: ClassVar[tuple[str, ...]] = (
        "stream_options",
        "min_tokens",
        "n",
        "best_of",
        "max_completion_tokens",
    )
    # True when each streamed decode chunk carries its token IDs.
    token_ids: ClassVar[bool] = True
    # Below the engine server's own idle close, so a reused connection is never closing.
    keepalive_expiry_s: ClassVar[float] = 4.0
    engine_output_fields: ClassVar[tuple[str, ...]] = ("token_ids", "prompt_token_ids")
    # Engine choice fields that a reassembled non-streaming choice carries from the stream.
    choice_fields: ClassVar[tuple[str, ...]] = ()
    # Dotted client fields that would override Narwhal's engine controls.
    reserved_fields: ClassVar[tuple[str, ...]] = ()
    # Chat template fields the tokenize route needs to count the engine's render.
    tokenize_passthrough: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]: ...

    @abstractmethod
    def token_id_fields(self) -> dict[str, Any]: ...

    def stream_refused_fields(self, endpoint: str) -> tuple[str, ...]:
        """Client fields the engine refuses on a streamed request to `endpoint`."""
        return ()

    def streams_token_ids(self, endpoint: str) -> bool:
        return self.token_ids

    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model}
        if "messages" in body:
            payload["messages"] = body["messages"]
            for field_name in self.tokenize_passthrough:
                if field_name in body:
                    payload[field_name] = body[field_name]
        else:
            payload["prompt"] = body.get("prompt", "")
            # The completion's own setting decides whether the engine adds BOS and similar tokens.
            if "add_special_tokens" in body:
                payload["add_special_tokens"] = body["add_special_tokens"]
        return payload

    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        try:
            return int(payload["count"])
        except (KeyError, TypeError, ValueError):
            return None

    def context_limit(self, payload: dict[str, Any], model: str) -> int | None:
        limit = payload.get("max_model_len")
        return limit if type(limit) is int and limit > 0 else None

    def tokenize_token_ids(self, payload: dict[str, Any]) -> list[int] | None:
        tokens = payload.get("tokens")
        if (
            not isinstance(tokens, list)
            or not non_negative_ints(tokens)
            or self.tokenize_response(payload) != len(tokens)
        ):
            return None
        return tokens

    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        return {"min_tokens": tokens, "ignore_eos": True}

    def peerless_fields(self) -> dict[str, Any] | None:
        """Fields that let a disaggregated engine serve a request in its role without a KV peer.

        None when every request needs an engine of the other role.
        """
        return None

    def cold_probe_extras(self) -> dict[str, Any]:
        # The engine keys its prefix cache by cache_salt, so a fresh salt shares no prefix.
        return {"cache_salt": secrets.token_urlsafe(32)}
