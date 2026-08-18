"""The serving engine's API dialect, named.

The scheduler core is portable by construction - no GPU term appears anywhere
in it - but a handful of HTTP details around Arrow §5.6's fixed protocol are the
engine build's own: the non-OpenAI /health and /tokenize routes, the
/tokenize response shape, the parameters a one-token prefill leg cannot
carry, and the `min_tokens`/`ignore_eos` the decode sweep relies on. Today
every one of those is vLLM's, baked into engine.py and probe.py. A dialect
packs them behind this module's small ABC and registry, selected by name in
the fleet config's `dialect` key - the connector seam mirrored one
layer up, and the admission path for SGLang or TRT-LLM without an engine.py
rewrite.

A build with no exact-count route sets `tokenize_path` to None. The router
then prices prefill cost off the character ratio, and the profiler sizes
prompts the same way and says so, instead of refusing to run at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class EngineDialect(ABC):
    """The build-specific requests and routes around the OpenAI-compatible core."""

    name: ClassVar[str]
    # GET answering 200 means serving. OpenAI carries no liveness route, so
    # every build's is its own.
    health_path: ClassVar[str] = "/health"
    # POST exact token counting; None means the build has none, and every
    # caller degrades to the character ratio rather than failing.
    tokenize_path: ClassVar[str | None] = "/tokenize"
    # Request parameters rejected alongside the prefill leg's max_tokens=1 /
    # stream=False; engine.py drops them so a client's own values cannot
    # fail the leg that places its work.
    prefill_incompatible: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """The exact-count payload for a serving body (`model` may be None)."""

    @abstractmethod
    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        """The count out of the route's response shape; None where it is absent."""

    @abstractmethod
    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        """Body keys holding the sweep's decode at exactly `tokens` per stream.

        The fit needs the batch fixed for the whole sample window, so an EOS
        before `tokens` must not end the stream and short output must not
        under-run it; how a build says that is its dialect's business.
        """


class VllmDialect(EngineDialect):
    """vLLM's serving layer: the reference build both fleets run today."""

    name = "vllm"
    prefill_incompatible = ("stream_options", "min_tokens", "n", "best_of")

    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """vLLM counts a completion's prompt or a chat body's messages."""
        payload: dict[str, Any] = {"model": model}
        if "messages" in body:
            payload["messages"] = body["messages"]
        else:
            payload["prompt"] = body.get("prompt", "")
        return payload

    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        """`{"count": N}`."""
        try:
            return int(payload["count"])
        except (KeyError, TypeError, ValueError):
            return None

    def decode_probe_extras(self, tokens: int) -> dict[str, Any]:
        """`min_tokens` keeps the stream alive past EOS; `ignore_eos` stops it ending early."""
        return {"min_tokens": tokens, "ignore_eos": True}


# The registry is small on purpose: a name joins it only against a build the
# gates have run on, not with code that merely exists.
_REGISTRY: dict[str, EngineDialect] = {d.name: d for d in (VllmDialect(),)}


def lookup(name: str) -> EngineDialect:
    """The dialect the config names, or a refusal naming it back."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown dialect {name!r}: known ones are {', '.join(sorted(_REGISTRY))}"
        ) from None
