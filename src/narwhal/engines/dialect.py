from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class EngineDialect(ABC):
    name: ClassVar[str]
    health_path: ClassVar[str] = "/health"
    # None selects character-ratio estimates in callers.
    tokenize_path: ClassVar[str | None] = "/tokenize"
    # None means the engine exposes no HTTP cache-reset capability.
    cache_reset_path: ClassVar[str | None] = None
    # These client fields conflict with the forced one-token prefill request.
    prefill_incompatible: ClassVar[tuple[str, ...]] = ()
    # True when streamed decode honors return_token_ids and stream_interval.
    token_ids: ClassVar[bool] = False
    # Below the engine server's own idle close, so a reused connection is never closing.
    keepalive_expiry_s: ClassVar[float] = 4.0
    engine_output_fields: ClassVar[tuple[str, ...]] = ()
    # Dotted client fields that would override Narwhal's engine controls.
    reserved_fields: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def request_id(self, rid: str) -> tuple[dict[str, str], dict[str, Any]]: ...

    @abstractmethod
    def token_id_fields(self) -> dict[str, Any]: ...

    @abstractmethod
    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def tokenize_response(self, payload: dict[str, Any]) -> int | None: ...

    @abstractmethod
    def tokenize_token_ids(self, payload: dict[str, Any]) -> list[int] | None: ...

    @abstractmethod
    def decode_probe_extras(self, tokens: int) -> dict[str, Any]: ...

    @abstractmethod
    def cold_probe_extras(self) -> dict[str, Any]: ...
