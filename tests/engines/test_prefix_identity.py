"""Check that request tokens and stored-block events name the same prefix blocks."""

import unittest

from narwhal.engines.kv_events import StoredBlocks, matched_identities, stored_identities
from narwhal.engines.prefix import CacheNamespace, block_identities

MODEL, TOKENIZER = "model", "contract"


def tokens(count, *, offset=0):
    return tuple(range(offset, offset + count))


class PrefixIdentityTests(unittest.TestCase):
    """Synthetic events follow the vLLM 0.29.0 stored-block payload."""

    def test_request_and_event_identities_match_across_chunked_prefill(self):
        """A prompt cached over two scheduler steps chains through the backend parent."""
        prompt = tokens(40)
        request = block_identities(CacheNamespace(MODEL, TOKENIZER), prompt, 8)
        self.assertEqual(len(request), 5)
        first = StoredBlocks((11, 12), None, prompt[:16], 8)
        second = StoredBlocks((13, 14, 15), 12, prompt[16:], 8)
        stored = stored_identities(first, MODEL, TOKENIZER, None)
        stored += stored_identities(second, MODEL, TOKENIZER, stored[-1])
        self.assertEqual(stored, request)

    def test_partial_final_block_has_no_identity(self):
        """Trailing tokens short of a full block have no identity."""
        namespace = CacheNamespace(MODEL, TOKENIZER)
        self.assertEqual(
            block_identities(namespace, tokens(23), 8), block_identities(namespace, tokens(16), 8)
        )
        self.assertEqual(block_identities(namespace, tokens(7), 8), [])
        self.assertIsNone(
            stored_identities(StoredBlocks((1,), None, tokens(12), 8), MODEL, "t", None)
        )

    def test_equal_token_counts_and_namespaces_do_not_imply_equal_prefixes(self):
        """Token values, block size, adapter, salt, model and tokenizer all separate identities."""
        base = block_identities(CacheNamespace(MODEL, TOKENIZER), tokens(16), 8)
        variants = [
            block_identities(CacheNamespace(MODEL, TOKENIZER), tokens(16, offset=1), 8),
            block_identities(CacheNamespace(MODEL, TOKENIZER), tokens(16), 4)[1::2],
            block_identities(CacheNamespace(MODEL, TOKENIZER, "adapter"), tokens(16), 8),
            block_identities(CacheNamespace(MODEL, TOKENIZER, cache_salt="salt"), tokens(16), 8),
            block_identities(CacheNamespace("other", TOKENIZER), tokens(16), 8),
            block_identities(CacheNamespace(MODEL, "other"), tokens(16), 8),
        ]
        for variant in variants:
            self.assertTrue(set(variant).isdisjoint(base))

    def test_salt_and_adapter_come_from_the_events_extra_keys(self):
        """vLLM mixes the salt into the first block and the adapter name into every block."""
        prompt = tokens(16)
        namespace = CacheNamespace(MODEL, TOKENIZER, "adapter", "salt")
        event = StoredBlocks(
            (1, 2),
            None,
            prompt,
            8,
            adapter="adapter",
            extra_keys=(("adapter", "salt"), ("adapter",)),
        )
        self.assertEqual(
            stored_identities(event, MODEL, TOKENIZER, None), block_identities(namespace, prompt, 8)
        )

    def test_unsupported_or_unknown_evidence_has_no_identity(self):
        """Multimodal keys, missing parents and skipped blocks cannot be named."""
        prompt = tokens(16)
        cases = [
            StoredBlocks((1, 2), None, prompt, 8, extra_keys=(("image-hash", 0), None)),
            StoredBlocks((1, 2), None, prompt, 8, extra_keys=(None, ("salt",))),
            StoredBlocks((1, 2), None, prompt, 8, extra_keys=(None,)),
            StoredBlocks((1, 2), None, prompt, 8, adapter="a", extra_keys=(("b",), ("b",))),
            StoredBlocks((2,), None, prompt, 8, kind="mamba"),
        ]
        for event in cases:
            with self.subTest(event=event):
                self.assertIsNone(stored_identities(event, MODEL, TOKENIZER, None))
        continued = StoredBlocks((3,), 2, tokens(8, offset=16), 8)
        self.assertIsNone(stored_identities(continued, MODEL, TOKENIZER, None))

    def test_skipping_groups_reuse_identities_from_a_complete_group(self):
        """A Mamba group's reported block takes the identity the attention group named."""
        prompt = tokens(24)
        attention = StoredBlocks((1, 2, 3), None, prompt, 8, group=3, kind="full_attention")
        mamba = StoredBlocks((3,), None, prompt, 8, group=0, kind="mamba")
        self.assertFalse(mamba.complete)
        named = dict(
            zip(
                attention.block_hashes,
                stored_identities(attention, MODEL, TOKENIZER, None),
                strict=True,
            )
        )
        self.assertEqual(matched_identities(mamba, named), [named[3]])
        self.assertEqual(matched_identities(StoredBlocks((9,), None, prompt, 8), named), [None])

    def test_token_ids_must_be_non_negative_64_bit_integers(self):
        namespace = CacheNamespace(MODEL, TOKENIZER)
        for bad in ([-1], [1 << 64], [True], ["1"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                block_identities(namespace, bad, 1)
        with self.assertRaises(ValueError):
            block_identities(namespace, [1], 0)
