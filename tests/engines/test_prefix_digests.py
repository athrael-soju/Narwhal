"""Pin prefix-block identities and token ID errors to the per-token reference implementation.

The golden digests were computed with the per-token `chain` implementation at commit cb41445.
"""

import enum
import hashlib
import unittest

from narwhal.engines.prefix import (
    CacheNamespace,
    block_identities,
    non_negative_ints,
    prefix_identities,
)

NAMESPACES = (
    CacheNamespace("model", "contract"),
    CacheNamespace("model", "contract", "adapter", "salt"),
    CacheNamespace("org/model-b", "0123456789abcdef", None, "sél ☃"),
)
BLOCK_SIZES = (1, 3, 16, 256)
# Partial final blocks, block-aligned prompts and lengths around HASH_THREAD_TOKENS (8192).
LENGTHS = (0, 1, 2, 15, 16, 17, 47, 48, 49, 1000, 8191, 8192, 8193, 8194, 8209)
# Token IDs spread across the full unsigned 64-bit range.
TOKENS = [0, (1 << 64) - 1, *((i * 0x9E3779B97F4A7C15) % (1 << 64) for i in range(2, 8209))]
PARENT = bytes(range(32))
TOKEN_ERROR = "token IDs must be non-negative 64-bit integers"

ROOT_16 = "bcde32e6f15aa14e97481b8ff9ff841dd86f9957a0b9c923aedc5bb56bf9748d"
FIRST_BLOCK_16 = "8ba62f91522fb7f4812ef381f54bd87b547cc2c4432667a6c4522a93bf8a0b20"
# Namespace index and block size to the fold of every length's identities.
GOLDEN_BLOCKS = {
    (0, 1): "55450f15340f78c3104fcd9a241c03dbc08e429f66d2fbadcade82bf5187ba2b",
    (0, 3): "2c13cf25a171bc405c53ddf66ca2e068f4d521cd0721cc5403a9cec3c48f29c0",
    (0, 16): "ce89da5bd6f219dde19ac943fa11a9457c25244f0caeea37a529af7e81620e98",
    (0, 256): "8d9b7b29da35f66d4a39b8ac1efca3ddf7f1f075806744dcdb3350227785d56d",
    (1, 1): "a2e0397a2fe689cd0fd263aa68c144d9a637dc39cf5927f2b32e6ce43d511546",
    (1, 3): "9cdb3da3483b81e9eff6dd251bad9e248e13256139d1c5a971c0ecc35761a50e",
    (1, 16): "3466479b283c5feecaec1bea74044411af53210a3ede3b01342c7fb81e54ca31",
    (1, 256): "c4f8e2b10b1b74bce7f88f22327fe5fb2772bf2f35d82723663eeef68b873a22",
    (2, 1): "897cf6c588f325aaa62e5375156b0f05481b078a0b771d9d67acab5bd20459e7",
    (2, 3): "81456de4a6cebb22f018ce82414a386fe8da26a50b142f5dc652c5209da00b2d",
    (2, 16): "7331a57746b2f942d0157c8ee63b7f1c8eb9470de71f26530b3b23eac195862a",
    (2, 256): "eb2d987144500e1ef16cfa1f3138702ab58dcf39e1aed4e8ca8330fca257783e",
}
# Block size to the fold of every length's identities chained from PARENT in namespace 0.
GOLDEN_PARENT = {
    1: "7e4a0cdbbb586a7a3b800fa59c5fa8e7074ee3c2d91073629da0abed95eca45b",
    3: "e5227964aaa488e2097c2340cab9252a87c06dfe3510bdeafd2d28ed66e7b866",
    16: "f4270533439ca72cf288c191a95875810a6953cd15727f770ba627801c104ea9",
    256: "f5667b37169fdbc56200c717fbbfd95fe4202c13cad69ef3ace2d476793893e3",
}
# Namespace index to the fold of each prompt's identities without its final token, per size.
GOLDEN_PROMPTS = {
    0: "bd7fa5d28b7bf5c128bd12674b498bad84a37ca62a7393a07e9fe83eb1916471",
    1: "0406937062d8379dbc96e30487cae25e9d1f6dceb9ab254719cc158c864d9444",
    2: "addb113b38dfbd5229a0d7cd90a4b8ddf82c2c8b6e78bbddb3b8a926efea2364",
}


class _Token(enum.IntEnum):
    ONE = 1


BAD_TOKENS = (-1, -(1 << 64), 1 << 64, 1 << 70, True, False, "1", 1.0, None, _Token.ONE)


def fold(identity_lists):
    """Digest identity lists with their lengths so block counts are pinned too."""
    digest = hashlib.sha256()
    for identities in identity_lists:
        digest.update(len(identities).to_bytes(4, "little"))
        digest.update(b"".join(identities))
    return digest.hexdigest()


def outcome(call):
    """Return the call's result, or its exception type and message."""
    try:
        return call()
    except Exception as exc:
        return type(exc), str(exc)


class PrefixDigestTests(unittest.TestCase):
    def test_identities_match_the_reference_digests(self):
        self.assertEqual(NAMESPACES[0].root(16).hex(), ROOT_16)
        self.assertEqual(block_identities(NAMESPACES[0], TOKENS[:16], 16)[0].hex(), FIRST_BLOCK_16)
        for (index, size), expected in GOLDEN_BLOCKS.items():
            with self.subTest(namespace=index, block_size=size):
                lists = (block_identities(NAMESPACES[index], TOKENS[:n], size) for n in LENGTHS)
                self.assertEqual(fold(lists), expected)
        for size, expected in GOLDEN_PARENT.items():
            with self.subTest(parent=True, block_size=size):
                lists = (
                    block_identities(NAMESPACES[0], tuple(TOKENS[:n]), size, parent=PARENT)
                    for n in LENGTHS
                )
                self.assertEqual(fold(lists), expected)

    def test_prompt_identities_match_the_reference_digests_without_the_final_token(self):
        for index, expected in GOLDEN_PROMPTS.items():
            with self.subTest(namespace=index):
                lists = []
                for n in LENGTHS[2:]:
                    by_size = prefix_identities(NAMESPACES[index], TOKENS[:n], n - 1, BLOCK_SIZES)
                    self.assertEqual(list(by_size), list(BLOCK_SIZES))
                    lists.extend(by_size.values())
                self.assertEqual(fold(lists), expected)

    def test_bad_tokens_raise_only_inside_hashed_blocks(self):
        """Ten tokens in blocks of four hash positions 0-7; positions 8 and 9 are never read."""
        namespace = NAMESPACES[1]
        valid = list(range(10))
        for parent in (None, PARENT):
            expected = block_identities(namespace, valid, 4, parent=parent)
            self.assertEqual(len(expected), 2)
            for bad in BAD_TOKENS:
                for position in range(10):
                    tokens = [*valid[:position], bad, *valid[position + 1 :]]
                    for sequence in (tokens, tuple(tokens)):
                        with self.subTest(
                            parent=parent, bad=bad, position=position, kind=type(sequence)
                        ):
                            got = outcome(
                                lambda s=sequence, p=parent: block_identities(
                                    namespace, s, 4, parent=p
                                )
                            )
                            self.assertEqual(
                                got, expected if position >= 8 else (ValueError, TOKEN_ERROR)
                            )

    def test_prompt_identities_check_only_the_longest_hashed_prefix(self):
        """Count 9 hashes positions 0-7 for blocks of four and 0-8 for blocks of three."""
        namespace = NAMESPACES[0]
        valid = list(range(10))
        expected = prefix_identities(namespace, valid, 9, (4, 3))
        self.assertEqual({size: len(ids) for size, ids in expected.items()}, {4: 2, 3: 3})
        for bad in BAD_TOKENS:
            for position in range(10):
                tokens = [*valid[:position], bad, *valid[position + 1 :]]
                with self.subTest(bad=bad, position=position):
                    got = outcome(lambda t=tokens: prefix_identities(namespace, t, 9, (4, 3)))
                    self.assertEqual(got, expected if position == 9 else (ValueError, TOKEN_ERROR))

    def test_block_size_errors_keep_their_order_and_messages(self):
        namespace = NAMESPACES[0]
        for size in (0, -1):
            with self.subTest(size=size):
                # The block size error precedes any token check.
                self.assertEqual(
                    outcome(lambda s=size: block_identities(namespace, [-1], s)),
                    (ValueError, "block size must be positive"),
                )
                self.assertEqual(
                    outcome(lambda s=size: prefix_identities(namespace, [1, 2], 1, (4, s))),
                    (ValueError, "block size must be positive"),
                )
        self.assertEqual(
            outcome(lambda: block_identities(namespace, [1, 2], 0, parent=PARENT)),
            (ValueError, "range() arg 3 must not be zero"),
        )
        self.assertEqual(block_identities(namespace, [1, 2], -1, parent=PARENT), [])

    def test_roots_separate_block_size_types(self):
        """A cached root for 1 or 8 never answers for True or 8.0."""
        namespace = NAMESPACES[0]
        self.assertNotEqual(namespace.root(1), namespace.root(True))
        self.assertNotEqual(namespace.root(8), namespace.root(8.0))

    def test_byte_strings_hash_as_their_integer_items(self):
        namespace = NAMESPACES[0]
        self.assertEqual(
            block_identities(namespace, b"\x01\x02\x03\x04", 2),
            block_identities(namespace, [1, 2, 3, 4], 2),
        )

    def test_non_negative_ints_rejects_bools_subclasses_and_negatives(self):
        for values, valid in (
            ([], True),
            ([0, 1 << 64, 1 << 70], True),
            ([1, -1], False),
            ([True], False),
            ([_Token.ONE], False),
            ([1, 1.0], False),
            (["1"], False),
            ([None], False),
        ):
            with self.subTest(values=values):
                self.assertIs(non_negative_ints(values), valid)


if __name__ == "__main__":
    unittest.main()
