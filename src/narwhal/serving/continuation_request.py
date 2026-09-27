"""Validate explicit continuation requests before admission or engine dispatch."""

from __future__ import annotations

from typing import Any

from ..engines.replay import ReplayQualification
from .policy import ContinuationPolicy

OPT_IN = "narwhal_continuation"

# Supply explicit settings on every attempt.
_SETTINGS: dict[str, bool | int] = {
    "stream": True,
    "n": 1,
    "temperature": 0,
    "presence_penalty": 0,
    "frequency_penalty": 0,
    "repetition_penalty": 1,
    "top_p": 1,
    "top_k": -1,
    "min_p": 0,
    "min_tokens": 0,
    "echo": False,
    "add_special_tokens": False,
    "skip_special_tokens": True,
    "spaces_between_special_tokens": True,
    "ignore_eos": False,
    "include_stop_str_in_output": False,
}
_ALLOWED = {
    OPT_IN,
    "model",
    "prompt",
    "max_tokens",
    "stop",
    "stop_token_ids",
    "return_token_ids",
    "stream_options",
    "stream_interval",
    *_SETTINGS,
}


class ContinuationRequestError(ValueError):
    """An explicit opt-in violates the qualified request or deployment contract."""

    def __init__(self, message: str, param: str = OPT_IN) -> None:
        super().__init__(message)
        self.param = param


class ContinuationUnavailable(RuntimeError):
    """The required replay qualification is unavailable for this request."""


class ContinuationOverloaded(RuntimeError):
    """Other requests currently own the router's retained-history capacity."""


def prepare_request(
    endpoint: str,
    body: dict[str, Any],
    policy: ContinuationPolicy,
    qualification: ReplayQualification | None,
) -> tuple[dict[str, Any], bool]:
    """Return a backend body and opt-in decision, with content-free rejections."""
    if OPT_IN not in body:
        return body, False
    if type(body[OPT_IN]) is not bool:
        raise ContinuationRequestError("narwhal_continuation must be a boolean")
    normal = {key: value for key, value in body.items() if key != OPT_IN}
    if not body[OPT_IN]:
        return normal, False
    if not policy.enabled:
        raise ContinuationRequestError("stream continuation is disabled")
    if endpoint != "/v1/completions":
        raise ContinuationRequestError("continuation requires the completion endpoint")
    if any(key not in _ALLOWED for key in body):
        raise ContinuationRequestError("request contains fields outside the continuation subset")
    if body.get("stream") is not True:
        raise ContinuationRequestError("continuation requires stream=true", "stream")
    prompt = body.get("prompt")
    if (
        not isinstance(prompt, list)
        or not prompt
        or any(type(token) is not int or token < 0 or token >= 1 << 64 for token in prompt)
    ):
        raise ContinuationRequestError(
            "continuation requires one nonempty flat array of token IDs", "prompt"
        )
    output = body.get("max_tokens")
    if type(output) is not int or output < 1:
        raise ContinuationRequestError(
            "continuation requires an explicit positive max_tokens", "max_tokens"
        )
    if len(prompt) + output > policy.max_context_tokens:
        raise ContinuationRequestError("continuation context limit exceeded", "max_tokens")
    for name, expected in _SETTINGS.items():
        if name in body:
            value = body[name]
            if type(expected) is bool:
                valid = type(value) is bool and value is expected
            elif name in ("n", "top_k", "min_tokens"):
                valid = type(value) is int and value == expected
            else:
                valid = type(value) in (int, float) and value == expected
            if not valid:
                raise ContinuationRequestError(
                    "generation setting is outside the continuation subset", name
                )
        normal[name] = expected
    if body.get("stop") not in (None, []):
        raise ContinuationRequestError("continuation does not support string stops", "stop")
    normal["stop"] = []
    stops = body.get("stop_token_ids", [])
    if not isinstance(stops, list) or any(type(token) is not int or token < 0 for token in stops):
        raise ContinuationRequestError("stop_token_ids must be a token ID array", "stop_token_ids")
    if "return_token_ids" in body and type(body["return_token_ids"]) is not bool:
        raise ContinuationRequestError("return_token_ids must be a boolean", "return_token_ids")
    if "stream_interval" in body and (
        type(body["stream_interval"]) is not int or body["stream_interval"] != 1
    ):
        raise ContinuationRequestError("continuation requires stream_interval=1", "stream_interval")
    options = body.get("stream_options")
    if options is not None:
        if not isinstance(options, dict) or any(
            key not in ("include_usage", "continuous_usage_stats") or type(value) is not bool
            for key, value in options.items()
        ):
            raise ContinuationRequestError(
                "unsupported continuation stream options", "stream_options"
            )
        if options.get("continuous_usage_stats", False):
            raise ContinuationRequestError(
                "continuation supports final usage only", "stream_options"
            )
    if qualification is None:
        raise ContinuationUnavailable("continuation qualification is unavailable")
    contract = qualification.contract
    if any(token >= contract.vocab_size for token in prompt):
        raise ContinuationRequestError("prompt ID is outside the qualified vocabulary", "prompt")
    if len(prompt) + output > contract.max_context_tokens or output > contract.max_output_tokens:
        raise ContinuationRequestError(
            "qualified backend output or context limit exceeded", "max_tokens"
        )
    if any(token not in contract.allowed_stop_token_ids for token in stops):
        raise ContinuationRequestError("token stop is not qualified", "stop_token_ids")
    normal["stop_token_ids"] = stops
    return normal, True
