from __future__ import annotations

import secrets
from typing import Any, ClassVar

from ...engines.dialect import EngineDialect
from ...engines.prefix import non_negative_ints
from .connector import BOOTSTRAP_FIELDS


class SglangDialect(EngineDialect):
    name = "sglang"
    cache_reset_path = "/flush_cache"
    # /tokenize reports the tokenizer's limit, not the --context-length the engine serves.
    context_limit_path = "/server_info"
    prefill_incompatible = ("stream_options", "min_tokens", "n", "best_of", "max_completion_tokens")
    # The launcher pins --stream-interval 1, so each streamed chunk carries one token.
    token_ids = True
    # SGLang's uvicorn server closes an idle connection after 5 s.
    keepalive_expiry_s = 4.0
    engine_output_fields = ("token_ids", "prompt_token_ids")
    # The stop token or string that ended the output.
    choice_fields = ("matched_stop",)
    # A client request ID or rendezvous would collide with the router's own.
    reserved_fields = ("rid", *BOOTSTRAP_FIELDS)
    tokenize_passthrough: ClassVar[tuple[str, ...]] = (
        "tools",
        "chat_template_kwargs",
        "add_generation_prompt",
        "add_special_tokens",
    )

    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]:
        # SGLang takes the request ID from the body and ignores X-Request-Id.
        return {}, {"rid": rid}

    def token_id_fields(self) -> dict[str, Any]:
        return {"return_token_ids": True}

    def streams_token_ids(self, endpoint: str) -> bool:
        # SGLang rejects return_token_ids on streamed chat, where each chunk still carries one
        # token under the pinned --stream-interval 1.
        return endpoint != "/v1/chat/completions"

    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model}
        if "messages" in body:
            payload["messages"] = body["messages"]
            for field_name in self.tokenize_passthrough:
                if field_name in body:
                    payload[field_name] = body[field_name]
        else:
            payload["prompt"] = body.get("prompt", "")
            if "add_special_tokens" in body:
                payload["add_special_tokens"] = body["add_special_tokens"]
        return payload

    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        try:
            return int(payload["count"])
        except (KeyError, TypeError, ValueError):
            return None

    def context_limit(self, payload: dict[str, Any], model: str) -> int | None:
        # SGLang rejects an input of max_req_input_len tokens or more, a few tokens short of
        # its context length, so a request fits when its input and one output token fit this.
        if payload.get("served_model_name") != model:
            return None
        context, inputs = payload.get("context_length"), payload.get("max_req_input_len")
        if type(context) is not int or type(inputs) is not int or min(context, inputs) <= 0:
            return None
        return min(context, inputs)

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

    def cold_probe_extras(self) -> dict[str, Any]:
        # SGLang keys its radix cache by cache_salt, so a fresh salt shares no prefix.
        return {"cache_salt": secrets.token_urlsafe(32)}
