from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

from .prefix import non_negative_ints


class EngineDialect(ABC):
    name: ClassVar[str]
    health_path: ClassVar[str] = "/health"
    # None selects character-ratio estimates in callers.
    tokenize_path: ClassVar[str | None] = "/tokenize"
    # A route that renders chat messages to prompt text, for an engine whose tokenize route
    # takes text only. None sends the messages to the tokenize route.
    render_path: ClassVar[str | None] = None
    # False when the tokenize route counts prompt text only. With no render route, callers then
    # estimate chat requests instead of counting them.
    tokenize_messages: ClassVar[bool] = True
    # None reads the context limit from the tokenize response.
    context_limit_path: ClassVar[str | None] = None
    # The launch flag that sets the context limit, for an engine that reports none. Callers
    # then read the limit from the engine's attested launch.
    context_limit_flag: ClassVar[str | None] = None
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
    # True when, without token IDs, each streamed choice chunk carries one token and the usage
    # frame that stream_options.include_usage adds counts them.
    one_token_chunks: ClassVar[bool] = False
    # True when `stream_output_tokens` reads a reply's output token count from its stream.
    reports_output_tokens: ClassVar[bool] = False
    # True when the stream reports that count only in the usage frame that
    # stream_options.include_usage adds. The router asks for that frame on each decode leg and
    # drops it from a streamed reply whose client did not ask for it.
    usage_frame_counts: ClassVar[bool] = False
    # Below the engine server's own idle close, so a reused connection is never closing.
    keepalive_expiry_s: ClassVar[float] = 4.0
    engine_output_fields: ClassVar[tuple[str, ...]] = ("token_ids", "prompt_token_ids")
    # Engine choice fields that a reassembled non-streaming choice carries from the stream.
    choice_fields: ClassVar[tuple[str, ...]] = ()
    # Dotted client fields that would override Narwhal's engine controls.
    reserved_fields: ClassVar[tuple[str, ...]] = ()
    # Chat template fields the tokenize route needs to count the engine's render.
    tokenize_passthrough: ClassVar[tuple[str, ...]] = ()
    # KV tokens the engine holds free for each running decode request beyond its prompt, when
    # that exceeds the request's output length.
    decode_reserved_tokens: ClassVar[int] = 0
    # Cache blocks the engine's reported KV capacity includes but never gives to a request.
    kv_reserved_blocks: ClassVar[int] = 0
    # Chat template fields the render route takes.
    render_passthrough: ClassVar[tuple[str, ...]] = ()
    # Request headers that make an engine without a KV transfer counter report a decode leg's
    # transfer in the leg's own reply, after its [DONE] terminator.
    transfer_report_headers: ClassVar[Mapping[str, str]] = {}

    @abstractmethod
    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]: ...

    @abstractmethod
    def token_id_fields(self) -> dict[str, Any]: ...

    def leg_transfer_seconds(self, payloads: list[dict[str, Any]]) -> float | None:
        """Seconds a decode leg spent receiving its KV cache, from the data payloads of a
        reply that `transfer_report_headers` requested, or None when the reply reports none.
        """
        return None

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

    def render_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": body["messages"]}
        for field_name in self.render_passthrough:
            if field_name in body:
                payload[field_name] = body[field_name]
        return payload

    def render_response(self, payload: dict[str, Any]) -> str | None:
        prompt = payload.get("prompt")
        return prompt if isinstance(prompt, str) else None

    def rendered_tokenize_request(
        self, model: str | None, prompt: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """The tokenize request that counts `prompt`, the render of the chat request `body`,
        as the engine counts that request.
        """
        return {"model": model, "prompt": prompt, "add_special_tokens": False}

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

    def stream_output_tokens(self, payload: dict[str, Any]) -> int | None:
        """The reply's output token count as a stream chunk reports it, for an engine whose
        chunks carry no token IDs; None for a chunk that reports none.
        """
        return None

    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        return {"min_tokens": tokens, "ignore_eos": True}

    def peerless_fields(self) -> dict[str, Any] | None:
        """Fields that let a disaggregated engine serve a request in its role without a KV peer.

        None when every request needs an engine of the other role.
        """
        return None

    def cache_reset_params(self, wait_s: float) -> dict[str, Any]:
        """Query parameters that make a cache reset wait up to `wait_s` for the engine to idle."""
        return {}

    def cold_probe_extras(self) -> dict[str, Any]:
        # The engine keys its prefix cache by cache_salt, so a fresh salt shares no prefix.
        return {"cache_salt": secrets.token_urlsafe(32)}
