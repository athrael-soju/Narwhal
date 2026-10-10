from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Hashable
from dataclasses import dataclass
from typing import ClassVar

from .prefix import CacheNamespace, block_identities

GPU_MEDIUM = "GPU"


@dataclass(frozen=True)
class StoredBlocks:
    block_hashes: tuple[Hashable, ...]
    parent_hash: Hashable | None
    token_ids: tuple[int, ...]
    block_size: int
    adapter: str | None = None
    # One entry per reported block hash.
    extra_keys: tuple[tuple[object, ...] | None, ...] | None = None
    group: int | None = None
    kind: str | None = None
    medium: str | None = None
    # Tokens a sliding-window group attends to; None for other groups.
    sliding_window: int | None = None

    @property
    def block_count(self) -> int:
        return len(self.token_ids) // self.block_size

    @property
    def complete(self) -> bool:
        return (
            self.block_size > 0
            and len(self.token_ids) == self.block_count * self.block_size
            and len(self.block_hashes) == self.block_count > 0
        )


@dataclass(frozen=True)
class RemovedBlocks:
    block_hashes: tuple[Hashable, ...]
    group: int | None = None
    medium: str | None = None


@dataclass(frozen=True)
class CacheCleared: ...


CacheEvent = StoredBlocks | RemovedBlocks | CacheCleared


def _block_extras(event: StoredBlocks, index: int) -> tuple[object, ...]:
    if event.extra_keys is None:
        return ()
    extras = event.extra_keys[index]
    return () if extras is None else tuple(extras)


def _cache_salt(event: StoredBlocks) -> tuple[bool, str | None]:
    if event.extra_keys is not None and len(event.extra_keys) != len(event.block_hashes):
        return False, None
    adapter = () if event.adapter is None else (event.adapter,)
    salt = None
    for index in range(len(event.block_hashes)):
        extras = _block_extras(event, index)
        if extras[: len(adapter)] != adapter:
            return False, None
        rest = extras[len(adapter) :]
        # The salt is mixed into the first prompt block only.
        if index == 0 and event.parent_hash is None and len(rest) == 1:
            if not isinstance(rest[0], str):
                return False, None
            salt = rest[0]
        elif rest:
            # Multimodal and prompt-embedding keys have no Narwhal identity.
            return False, None
    return True, salt


def stored_identities(
    event: StoredBlocks, model: str, tokenizer: str, parent: bytes | None
) -> list[bytes] | None:
    if not event.complete or (event.parent_hash is not None and parent is None):
        return None
    supported, salt = _cache_salt(event)
    if not supported:
        return None
    namespace = CacheNamespace(model, tokenizer, event.adapter, salt)
    return block_identities(
        namespace,
        event.token_ids,
        event.block_size,
        parent=None if event.parent_hash is None else parent,
    )


def matched_identities(event: StoredBlocks, known: dict[Hashable, bytes]) -> list[bytes | None]:
    return [known.get(block_hash) for block_hash in event.block_hashes]


class KvEventDecoder(ABC):
    # The engine always computes a prompt's final token, so a cached prefix never covers it.
    recomputes_final_token: ClassVar[bool] = True

    @abstractmethod
    def decode_batch(self, payload: bytes) -> list[CacheEvent | None]: ...
