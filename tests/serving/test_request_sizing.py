import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from narwhal.config.model import EngineContract
from narwhal.engines.prefix import CacheNamespace
from narwhal.serving.app import create_app
from narwhal.serving.router import sizing
from tests.fixtures import fleet, hold_prefix

BLOCK = 4


class RequestSizingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.cfg.tokenize = False
        self.router = create_app(self.cfg).state.router
        self.sizer = self.router.sizer
        self.iid = self.cfg.engines[0].iid
        self.prompt = list(range(18))

    def hold(self, tokens, contract=None):
        contract = contract or self.cfg.engine_contract
        namespace = CacheNamespace(self.cfg.model, contract.fingerprint())
        hold_prefix(self.router.residency.view(self.iid), namespace, tokens, BLOCK)

    def fingerprints(self):
        return patch.object(
            EngineContract, "fingerprint", autospec=True, side_effect=EngineContract.fingerprint
        )

    def test_each_contract_is_fingerprinted_once(self):
        contract = self.cfg.engine_contract
        other = replace(contract, engine_version=f"{contract.engine_version}-other")
        self.assertNotEqual(other.fingerprint(), contract.fingerprint())
        self.hold(self.prompt)
        with self.fingerprints() as spy:
            for _ in range(3):
                self.assertEqual(
                    self.sizer.prefix_cache_evidence({}, self.prompt)[0], {self.iid: 16}
                )
            self.assertEqual(spy.call_count, 1)
            self.router.cfg.engine_contract = other
            self.assertEqual(self.sizer.prefix_cache_evidence({}, self.prompt)[0], {})
            self.assertEqual(spy.call_count, 2)
        self.hold(self.prompt, other)
        self.assertEqual(self.sizer.prefix_cache_evidence({}, self.prompt)[0], {self.iid: 16})

    def test_no_known_block_size_skips_the_fingerprint(self):
        self.assertEqual(self.router.residency.block_sizes(), set())
        with self.fingerprints() as spy:
            self.assertEqual(self.sizer.prefix_cache_evidence({}, self.prompt), ({}, {}, {}))
            spy.assert_not_called()

    def test_only_hashed_token_ids_must_fit_an_identity(self):
        self.hold(self.prompt)
        expected = self.sizer.prefix_cache_evidence({}, self.prompt)
        self.assertEqual(expected[0], {self.iid: 16})
        for position in range(18):
            tokens = [*self.prompt[:position], 1 << 64, *self.prompt[position + 1 :]]
            with self.subTest(position=position):
                self.assertEqual(
                    self.sizer.prefix_cache_evidence({}, tokens),
                    expected if position >= 16 else ({}, {}, {}),
                )

    def test_token_prompts_need_non_negative_int_ids(self):
        self.hold(self.prompt)
        evidence = self.sizer.prefix_cache_evidence({}, self.prompt)
        sized = asyncio.run(self.sizer.size({"prompt": self.prompt}))
        self.assertEqual(sized, (18, *evidence))
        for bad in (-1, True, 1.0, "1", None):
            prompt = [*self.prompt[:-1], bad]
            with self.subTest(bad=bad):
                self.assertEqual(asyncio.run(self.sizer.size({"prompt": prompt})), (18, {}, {}, {}))

    def test_prompts_longer_than_the_thread_threshold_hash_in_a_worker_thread(self):
        length = sizing.HASH_THREAD_TOKENS
        self.hold(list(range(length + 1)))
        for tokens, threaded in ((length, False), (length + 1, True)):
            prompt = list(range(tokens))
            with (
                self.subTest(tokens=tokens),
                patch.object(sizing.asyncio, "to_thread", wraps=asyncio.to_thread) as spy,
            ):
                evidence = asyncio.run(self.sizer._cache_evidence({}, prompt))
                self.assertEqual(spy.called, threaded)
                self.assertEqual(evidence, self.sizer.prefix_cache_evidence({}, prompt))
                self.assertEqual(evidence[0], {self.iid: (tokens - 1) // BLOCK * BLOCK})


if __name__ == "__main__":
    unittest.main()
