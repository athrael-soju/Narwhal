from __future__ import annotations

import secrets
from typing import Any

from ...engines.dialect import EngineDialect


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
    tokenize_passthrough = (
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

    def cold_probe_extras(self) -> dict[str, Any]:
        # vLLM hashes cache_salt into the first block; later block hashes chain from it.
        return {"cache_salt": secrets.token_urlsafe(32)}
