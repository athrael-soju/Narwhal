from __future__ import annotations

from typing import Any

from ...engines.dialect import EngineDialect


class VllmDialect(EngineDialect):
    name = "vllm"
    # vLLM exposes this route only with VLLM_SERVER_DEV_MODE=1.
    cache_reset_path = "/reset_prefix_cache"
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
        # vLLM streams per-chunk token ids for return_token_ids and honors stream_interval=1.
        return {"return_token_ids": True, "stream_interval": 1}
