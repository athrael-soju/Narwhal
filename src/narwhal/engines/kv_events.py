from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any, ClassVar

import msgpack  # type: ignore[import-untyped]

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


def block_hashes(values: Any) -> tuple[Hashable, ...]:
    if not isinstance(values, list) or not all(isinstance(v, bytes | int) for v in values):
        raise ValueError("block hashes must be a list of bytes or integers")
    return tuple(values)


class KvEventDecoder(ABC):
    # The engine always computes a prompt's final token, so a cached prefix never covers it.
    recomputes_final_token: ClassVar[bool] = True
    # Shortest block the residency index publishes; shorter pages publish as runs of pages.
    min_block_tokens: ClassVar[int | None] = None

    @abstractmethod
    def decode_batch(self, payload: bytes) -> list[CacheEvent | None]: ...


class MsgpackKvEvents(KvEventDecoder):
    # The batch layout the shape error names.
    batch_shape: ClassVar[str] = "[timestamp, events, ...]"

    @abstractmethod
    def stored(
        self, item: dict[str, Any], parent: Hashable | None, tokens: list[int]
    ) -> StoredBlocks: ...

    def removed_group(self, item: dict[str, Any]) -> int | None:
        return None

    def decode_batch(self, payload: bytes) -> list[CacheEvent | None]:
        try:
            batch = msgpack.unpackb(payload, raw=False, strict_map_key=False)
            if not isinstance(batch, list) or len(batch) < 2 or not isinstance(batch[1], list):
                raise ValueError(f"event batch must be {self.batch_shape}")
            return [self._event(item) for item in batch[1]]
        except (msgpack.UnpackException, KeyError, TypeError) as exc:
            raise ValueError(f"malformed cache event batch: {exc}") from exc

    def _event(self, item: Any) -> CacheEvent | None:
        if not isinstance(item, dict):
            raise ValueError("cache event must be a map")
        kind = item.get("type")
        if kind == "AllBlocksCleared":
            return CacheCleared()
        if kind == "BlockRemoved":
            hashes = block_hashes(item["block_hashes"])
            return RemovedBlocks(hashes, self.removed_group(item), item.get("medium"))
        if kind != "BlockStored":
            return None
        parent = item.get("parent_block_hash")
        tokens = item["token_ids"]
        if not isinstance(tokens, list) or any(type(t) is not int for t in tokens):
            raise ValueError("stored-block token IDs must be integers")
        if parent is not None and not isinstance(parent, bytes | int):
            raise ValueError("parent block hash must be bytes or an integer")
        return self.stored(item, parent, tokens)
