from __future__ import annotations

import secrets
from typing import Any, ClassVar

from ...engines.dialect import EngineDialect
from ...engines.prefix import non_negative_ints


class VllmDialect(EngineDialect):
    name = "vllm"
    # vLLM exposes this route only with VLLM_SERVER_DEV_MODE=1.
    cache_reset_path = "/reset_prefix_cache"
    prefill_incompatible = ("stream_options", "min_tokens", "n", "best_of", "max_completion_tokens")
    # vLLM streams per-chunk token ids for return_token_ids and honors stream_interval=1.
    token_ids = True
    # vLLM closes an idle connection after 5 s (VLLM_HTTP_TIMEOUT_KEEP_ALIVE).
    keepalive_expiry_s = 4.0
    engine_output_fields = ("token_ids", "prompt_token_ids")
    # Full cache reports mark empty sliding-window and Mamba positions as resident.
    # Transfer parameters replace the router's own KV and encoder-cache handoff.
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
        try:
            return int(payload["count"])
        except (KeyError, TypeError, ValueError):
            return None

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
        # vLLM hashes cache_salt into the first block; later block hashes chain from it.
        return {"cache_salt": secrets.token_urlsafe(32)}
