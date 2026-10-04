"""Narwhal prefix-block identity for exact prompt tokens.

An identity covers one full block's tokens, every earlier token, the block size
and the cache namespace, independent of the backend's hash algorithm or seed.
"""

from __future__ import annotations

import hashlib
import json
import sys
from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import repeat
from operator import is_
from typing import Any

_VERSION = b"narwhal-prefix-block-v1"
_ID_ERROR = "token IDs must be non-negative 64-bit integers"


@dataclass(frozen=True)
class CacheNamespace:
    """Inputs outside the token sequence that decide whether blocks are shareable.

    `model` and `tokenizer` are fleet-wide identity strings, such as the served
    model and the fleet engine contract.
    """

    model: str
    tokenizer: str
    adapter: str | None = None
    cache_salt: str | None = None

    def root(self, block_size: int) -> bytes:
        """Return the identity every first block chains from."""
        return _root(self.model, self.tokenizer, self.adapter, self.cache_salt, block_size)


@lru_cache(maxsize=1024, typed=True)
def _root(
    model: str, tokenizer: str, adapter: str | None, cache_salt: str | None, block_size: int
) -> bytes:
    if block_size < 1:
        raise ValueError("block size must be positive")
    fields = [model, tokenizer, adapter, cache_salt, block_size]
    return hashlib.sha256(_VERSION + json.dumps(fields).encode()).digest()


def non_negative_ints(values: Sequence[Any]) -> bool:
    """Return whether every value is a non-negative `int`; `bool` and other subclasses fail."""
    return all(map(is_, map(type, values), repeat(int))) and min(values, default=0) >= 0


def _pack(token_ids: Sequence[int], count: int) -> memoryview:
    """Return the first `count` token IDs as unsigned 64-bit little-endian values."""
    tokens = token_ids[:count] if count < len(token_ids) else token_ids
    if not isinstance(tokens, list | tuple):
        tokens = list(tokens)
    if not all(map(is_, map(type, tokens), repeat(int))):
        raise ValueError(_ID_ERROR)
    try:
        packed = array("Q", tokens)
    except OverflowError:
        raise ValueError(_ID_ERROR) from None
    if sys.byteorder == "big":
        packed.byteswap()
    return memoryview(packed)


def _chain(current: bytes, packed: memoryview, starts: range, block_size: int) -> list[bytes]:
    """Return the identity of each block at `starts`, chained from `current`."""
    identities = []
    for start in starts:
        digest = hashlib.sha256(current)
        digest.update(packed[start : start + block_size])
        current = digest.digest()
        identities.append(current)
    return identities


def block_identities(
    namespace: CacheNamespace,
    token_ids: Sequence[int],
    block_size: int,
    *,
    parent: bytes | None = None,
) -> list[bytes]:
    """Return one identity per full block of `token_ids`.

    Without `parent`, the tokens start at the first prompt token.
    """
    current = namespace.root(block_size) if parent is None else parent
    starts = range(0, len(token_ids) - block_size + 1, block_size)
    return _chain(current, _pack(token_ids, len(starts) * block_size), starts, block_size)


def prefix_identities(
    namespace: CacheNamespace, token_ids: Sequence[int], count: int, block_sizes: Iterable[int]
) -> dict[int, list[bytes]]:
    """Return the identities of the full blocks in the first `count` prompt tokens per block size.

    Raises ValueError when any block size is below 1 or any hashed token ID is invalid.
    """
    starts = {
        size: (namespace.root(size), range(0, count - size + 1, size)) for size in block_sizes
    }
    packed = _pack(token_ids, max((len(r) * size for size, (_, r) in starts.items()), default=0))
    return {size: _chain(root, packed, r, size) for size, (root, r) in starts.items()}
