"""Narwhal prefix-block identity for exact prompt tokens.

An identity names one full cache block by its tokens, every earlier token,
the block size and the cache namespace. Narwhal computes it the same way
from request token IDs and from an engine's stored-block events, so the two
can be compared without the backend's internal hash algorithm or seed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

_VERSION = b"narwhal-prefix-block-v1"
_TOKEN_BYTES = 8


@dataclass(frozen=True)
class CacheNamespace:
    """Inputs outside the token sequence that decide whether blocks are shareable.

    `model` and `tokenizer` are identity strings that every engine in a fleet
    shares, such as the served model and the fleet engine contract. `adapter`
    names a LoRA adapter and `cache_salt` carries a request's cache salt.
    """

    model: str
    tokenizer: str
    adapter: str | None = None
    cache_salt: str | None = None

    def root(self, block_size: int) -> bytes:
        """Return the identity every first block chains from."""
        if block_size < 1:
            raise ValueError("block size must be positive")
        fields = [self.model, self.tokenizer, self.adapter, self.cache_salt, block_size]
        return hashlib.sha256(_VERSION + json.dumps(fields).encode()).digest()


def chain(parent: bytes, block: Sequence[int]) -> bytes:
    """Return the identity of `block` following the block named by `parent`."""
    digest = hashlib.sha256(parent)
    for token in block:
        if type(token) is not int or not 0 <= token < 1 << (8 * _TOKEN_BYTES):
            raise ValueError("token IDs must be non-negative 64-bit integers")
        digest.update(token.to_bytes(_TOKEN_BYTES, "little"))
    return digest.digest()


def block_identities(
    namespace: CacheNamespace,
    token_ids: Sequence[int],
    block_size: int,
    *,
    parent: bytes | None = None,
) -> list[bytes]:
    """Return one identity per full block of `token_ids`.

    Without `parent`, the tokens start at the first prompt token. A partial
    final block has no identity because engines cache only full blocks.
    """
    current = namespace.root(block_size) if parent is None else parent
    identities = []
    for start in range(0, len(token_ids) - block_size + 1, block_size):
        current = chain(current, token_ids[start : start + block_size])
        identities.append(current)
    return identities
