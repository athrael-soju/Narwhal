from __future__ import annotations

import secrets
from typing import Any

from ...engines.dialect import EngineDialect
from .connector import BOOTSTRAP_FIELDS, FAKE_BOOTSTRAP_HOST
from .identity import SERVER_INFO_PATH


class SglangDialect(EngineDialect):
    name = "sglang"
    cache_reset_path = "/flush_cache"
    # /tokenize reports the tokenizer's limit, not the --context-length the engine serves.
    context_limit_path = SERVER_INFO_PATH
    # The stop token or string that ended the output.
    choice_fields = ("matched_stop",)
    # A client request ID or rendezvous would collide with the router's own.
    reserved_fields = ("rid", *BOOTSTRAP_FIELDS)
    tokenize_passthrough = (
        "tools",
        "chat_template_kwargs",
        "add_generation_prompt",
        "add_special_tokens",
    )

    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]:
        # SGLang takes the request ID from the body and ignores X-Request-Id.
        return {}, {"rid": rid}

    def peerless_fields(self) -> dict[str, Any]:
        # The room keys the request in the engine's transfer state, so each request takes its own.
        return {"bootstrap_host": FAKE_BOOTSTRAP_HOST, "bootstrap_room": secrets.randbits(63)}

    def cache_reset_params(self, wait_s: float) -> dict[str, Any]:
        # SGLang refuses a flush while it holds requests; with a timeout it waits for them first.
        return {"timeout": wait_s}

    def token_id_fields(self) -> dict[str, Any]:
        # The launcher pins --stream-interval 1, so each streamed chunk carries one token.
        return {"return_token_ids": True}

    def stream_refused_fields(self, endpoint: str) -> tuple[str, ...]:
        if endpoint == "/v1/chat/completions":
            return ("return_token_ids", "return_prompt_token_ids", "return_meta_info")
        return ()

    def streams_token_ids(self, endpoint: str) -> bool:
        # SGLang rejects return_token_ids on streamed chat, where each chunk still carries one
        # token under the pinned --stream-interval 1.
        return endpoint != "/v1/chat/completions"

    def context_limit(self, payload: dict[str, Any], model: str) -> int | None:
        # SGLang rejects an input of max_req_input_len tokens or more, a few tokens short of
        # its context length, so a request fits when its input and one output token fit this.
        if payload.get("served_model_name") != model:
            return None
        context, inputs = payload.get("context_length"), payload.get("max_req_input_len")
        if type(context) is not int or type(inputs) is not int or min(context, inputs) <= 0:
            return None
        return min(context, inputs)
