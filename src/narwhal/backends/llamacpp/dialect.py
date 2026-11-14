from __future__ import annotations

from typing import Any

from ...engines.dialect import EngineDialect
from .connector import SEAT_FIELD
from .identity import PROPS_PATH, context_tokens


class LlamacppDialect(EngineDialect):
    name = "llamacpp"
    # /tokenize takes text only; /apply-template renders chat messages to that text.
    render_path = "/apply-template"
    render_passthrough = ("tools", "chat_template_kwargs")
    context_limit_path = PROPS_PATH
    # Stream chunks carry text and no token IDs.
    token_ids = False
    reports_output_tokens = True
    # The connector assigns the slot and keeps prompt caching on for the handoff.
    # n_predict outranks the max_tokens that bounds a prefill leg to one token.
    reserved_fields = (SEAT_FIELD, "cache_prompt", "n_predict")

    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]:
        # The server assigns its own completion ID; the header reaches its access log only.
        return {"x-request-id": rid}, {}

    def token_id_fields(self) -> dict[str, Any]:
        return {}

    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        # A completion adds BOS and parses special tokens in its prompt; the count matches it.
        return {"content": body.get("prompt", ""), "add_special": True, "parse_special": True}

    def rendered_tokenize_request(
        self, model: str | None, prompt: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        # The template writes special tokens as text, and the server parses them when it
        # tokenizes a chat prompt.
        return {"content": prompt, "add_special": True, "parse_special": True}

    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        return len(tokens) if isinstance(tokens, list) else None

    def context_limit(self, payload: dict[str, Any], model: str) -> int | None:
        return context_tokens(payload)

    def stream_output_tokens(self, payload: dict[str, Any]) -> int | None:
        # The last chunk of every stream carries the server's timings.
        timings = payload.get("timings")
        count = timings.get("predicted_n") if isinstance(timings, dict) else None
        return count if type(count) is int and count >= 0 else None

    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        return {"ignore_eos": True}

    def cold_probe_extras(self) -> dict[str, Any]:
        # The server keys its prompt cache by slot content and takes no per-request salt.
        return {}
