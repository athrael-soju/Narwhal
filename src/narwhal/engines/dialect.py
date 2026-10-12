from __future__ import annotations

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
    prefill_incompatible: ClassVar[tuple[str, ...]] = ()
    # True when streamed decode honors return_token_ids and stream_interval.
    token_ids: ClassVar[bool] = False
    # Below the engine server's own idle close, so a reused connection is never closing.
    keepalive_expiry_s: ClassVar[float] = 4.0
    engine_output_fields: ClassVar[tuple[str, ...]] = ()
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

    def streams_token_ids(self, endpoint: str) -> bool:
        """Whether streamed decode on `endpoint` carries token IDs."""
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
        """The served context limit in a context-limit or tokenize response."""
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

    @abstractmethod
    def cold_probe_extras(self) -> dict[str, Any]: ...
