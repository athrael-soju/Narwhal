import importlib
from dataclasses import dataclass, field
from typing import Any

from narwhal.engines.kv_events import CacheEvent


@dataclass(frozen=True)
class ContractFixtures:
    # A metrics scrape and the values the backend's mapping reads from it.
    metrics: str
    kv_capacity: int
    cache_block_tokens: int
    prefix_cache_hits: int
    transfer_totals: tuple[float, float] | None
    # Cache event batches as published, with the event type each one decodes to.
    kv_batches: list[tuple[bytes, list[type[CacheEvent] | None]]]
    malformed_kv_batch: bytes
    # A verified attestation payload and the limits the identity reader takes from it.
    attestation: dict[str, Any]
    sequence_limit: int | None
    kv_lease: int | None
    memory_args: list[str]
    memory_fraction: str
    kv_event_args: list[str] = field(default_factory=list)
    no_kv_event_args: list[str] = field(default_factory=list)
    # Distributions an engine image reports to discovery, and the .env fields discovery reads.
    image_packages: dict[str, str] = field(default_factory=dict)
    discovery_settings: dict[str, str] = field(default_factory=dict)


def fixtures(backend: str) -> ContractFixtures:
    return importlib.import_module(f"tests.backends.fixtures.{backend}").FIXTURES
