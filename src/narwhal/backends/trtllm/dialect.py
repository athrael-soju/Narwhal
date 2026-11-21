from __future__ import annotations

from typing import Any

from ...engines.dialect import EngineDialect
from .connector import PARAMS, PROMPT_IDS


class TrtllmDialect(EngineDialect):
    name = "trtllm"
    tokenize_path = "/_internal/tokenize"
    # The tokenize route takes prompt text only, and the server has no chat render route.
    tokenize_messages = False
    # No route reports the context limit; the launch sets it.
    context_limit_flag = "--max_seq_len"
    # Stream chunks carry text, one token each, and no token IDs.
    token_ids = False
    one_token_chunks = True
    reports_output_tokens = True
    # Each stream chunk's choice reports the tokens the iteration decoded.
    choice_fields = ("avg_decoded_tokens_per_iter",)
    # The connector sets both fields for the generation leg.
    reserved_fields = (PARAMS, PROMPT_IDS)

    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]:
        # The server assigns its own completion ID and refuses unknown body fields.
        return {"x-request-id": rid}, {}

    def token_id_fields(self) -> dict[str, Any]:
        return {}

    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        return {"model": model, "prompt": body.get("prompt", "")}

    def stream_output_tokens(self, payload: dict[str, Any]) -> int | None:
        # The usage frame that stream_options.include_usage adds ends the stream.
        usage = payload.get("usage")
        count = usage.get("completion_tokens") if isinstance(usage, dict) else None
        return count if type(count) is int and count >= 0 else None

    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        return {"ignore_eos": True}

    def cold_probe_extras(self) -> dict[str, Any]:
        # The server takes cache_salt on chat requests only, and the probes send completions.
        return {}
