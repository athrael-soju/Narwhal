from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

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
    usage_frame_counts = True
    # The reply then ends, after [DONE], with a trtllm.perf_metrics event whose Server-Timing
    # field names the leg's KV transfer.
    transfer_report_headers: ClassVar[Mapping[str, str]] = MappingProxyType(
        {"X-TRTLLM-return-metrics": "1"}
    )
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

    def leg_transfer_seconds(self, payloads: list[dict[str, Any]]) -> float | None:
        # Server-Timing lists "<phase>_kv_transfer;dur=<ms>" among the leg's timings, and an
        # entry may carry other parameters.
        for payload in payloads:
            timing = payload.get("Server-Timing")
            if not isinstance(timing, str):
                continue
            for entry in timing.split(","):
                name, *params = (part.strip() for part in entry.split(";"))
                if not name.endswith("kv_transfer"):
                    continue
                for param in params:
                    key, _, value = param.partition("=")
                    if key.strip() != "dur":
                        continue
                    try:
                        seconds = float(value) / 1000
                    except ValueError:
                        continue
                    if seconds > 0 and math.isfinite(seconds):
                        return seconds
        return None

    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        return {"ignore_eos": True}

    def cold_probe_extras(self) -> dict[str, Any]:
        # The server takes cache_salt on chat requests only, and the probes send completions.
        return {}
