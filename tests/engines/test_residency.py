"""Check residency tracking against vLLM-shaped cache events and ZeroMQ sockets."""

import random
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path

import msgpack
import zmq

from narwhal.engines.kv_events import (
    CacheCleared,
    RemovedBlocks,
    decode_batch,
)
from narwhal.engines.prefix import CacheNamespace, block_identities
from narwhal.engines.residency import ResidencyIndex, cached_prefix_blocks
from narwhal.engines.residency_feed import ResidencyFeed

MODEL, TOKENIZER = "model", "contract"


def stored(hashes, tokens, *, parent=None, group=0, kind="full_attention", size=4, **fields):
    return {
        "type": "BlockStored",
        "block_hashes": list(hashes),
        "parent_block_hash": parent,
        "token_ids": list(tokens),
        "block_size": size,
        "lora_id": None,
        "medium": "GPU",
        "lora_name": None,
        "group_idx": group,
        "kv_cache_spec_kind": kind,
        **fields,
    }


def batch(*events):
    return msgpack.packb([1.0, list(events), None])


def identities(tokens, size=4):
    return block_identities(CacheNamespace(MODEL, TOKENIZER), tokens, size)


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class FakePublisher:
    """Publish numbered batches and serve replay requests like vLLM's ZeroMQ publisher."""

    def __init__(self, directory, *, buffer=100, replay=True, replay_gap_s=0.0, replay_limit=None):
        self.replay_gap_s = replay_gap_s
        # Emulate a ROUTER that drops a long replay's tail and its end marker.
        self.replay_limit = replay_limit
        self.endpoint = f"ipc://{directory}/events.sock"
        self.replay_endpoint = f"ipc://{directory}/replay.sock" if replay else None
        self.context = zmq.Context()
        self.pub = self.context.socket(zmq.PUB)
        self.pub.bind(self.endpoint)
        self.buffer = deque(maxlen=buffer)
        self.sequence = 0
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        if replay:
            self.router = self.context.socket(zmq.ROUTER)
            self.router.bind(self.replay_endpoint)
            self.thread = threading.Thread(target=self.serve_replay, daemon=True)
            self.thread.start()

    def publish(self, payload, *, deliver=True):
        with self.lock:
            sequence = self.sequence
            self.sequence += 1
            self.buffer.append((sequence, payload))
            if deliver:
                self.pub.send_multipart([b"", sequence.to_bytes(8, "big"), payload])

    def serve_replay(self):
        while not self.stopped.is_set():
            if not self.router.poll(50):
                continue
            client, _, start = self.router.recv_multipart()
            with self.lock:
                sent = 0
                for sequence, payload in list(self.buffer):
                    if sequence >= int.from_bytes(start, "big"):
                        if self.replay_limit is not None and sent == self.replay_limit:
                            break
                        time.sleep(self.replay_gap_s)
                        self.router.send_multipart(
                            [client, b"", sequence.to_bytes(8, "big"), payload]
                        )
                        sent += 1
                else:
                    self.router.send_multipart(
                        [client, b"", (-1).to_bytes(8, "big", signed=True), b""]
                    )

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()
            self.router.close(0)
        self.pub.close(0)
        self.context.term()


class DecodeTests(unittest.TestCase):
    def test_batch_decodes_vllm_event_maps(self):
        """Stored, removed and cleared events keep the fields residency needs."""
        events = decode_batch(
            batch(
                stored([b"h1"], range(4), extra_keys=[["salt"]]),
                {"type": "BlockRemoved", "block_hashes": [b"h1"], "medium": "GPU", "group_idx": 0},
                {"type": "AllBlocksCleared"},
                {"type": "FutureEvent"},
            )
        )
        self.assertEqual(events[0].block_hashes, (b"h1",))
        self.assertEqual(events[0].extra_keys, (("salt",),))
        self.assertEqual((events[0].group, events[0].kind), (0, "full_attention"))
        self.assertEqual(events[1], RemovedBlocks((b"h1",), 0, "GPU"))
        self.assertIsInstance(events[2], CacheCleared)
        self.assertIsNone(events[3])
        for payload in (b"\xc1", msgpack.packb({"events": []}), batch({"type": "BlockStored"})):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_batch(payload)


class ResidencyIndexTests(unittest.TestCase):
    def apply(self, index, sequence, *events):
        index.apply(sequence, decode_batch(batch(*events)))

    def test_complete_history_names_blocks_and_hybrid_prefixes(self):
        """Attention holds every block; Mamba holds the boundary state it reported."""
        index = ResidencyIndex(MODEL, TOKENIZER)
        prompt = tuple(range(12))
        self.apply(
            index,
            0,
            # vLLM lists the Mamba group before the attention group that names its block.
            stored([3], prompt, group=0, kind="mamba"),
            stored([1, 2, 3], prompt, group=1),
        )
        names = identities(prompt)
        self.assertTrue(index.known)
        self.assertEqual(index.cached_prefix_blocks(identities((*prompt, 7, 7, 7, 7))), 3)
        # Without Mamba state at block 2, a two-block prefix gives the engine nothing to reuse.
        self.assertEqual(index.cached_prefix_blocks(identities(prompt[:8] + (9,) * 4)), 0)
        snapshot = index.snapshot()
        self.assertEqual(snapshot["sequence"], 0)
        groups = {group["group"]: group for group in snapshot["groups"]}
        self.assertEqual(groups[1]["identities"], [name.hex() for name in names])
        self.assertEqual(groups[0]["identities"], [names[2].hex()])

    def test_eviction_reset_and_ordered_changes(self):
        index = ResidencyIndex(MODEL, TOKENIZER)
        prompt = tuple(range(8))
        names = identities(prompt)
        self.apply(index, 0, stored([1, 2], prompt))
        self.apply(index, 1, {"type": "BlockRemoved", "block_hashes": [2], "group_idx": 0})
        self.assertEqual(index.cached_prefix_blocks(names), 1)
        sequence, changes = index.changes_after(0)
        self.assertEqual(sequence, 1)
        self.assertEqual(changes[0]["groups"]["0"]["removed"], [names[1].hex()])
        self.assertEqual(index.changes_after(1), (1, []))
        self.assertIsNone(index.changes_after(5))
        self.apply(index, 2, {"type": "AllBlocksCleared"})
        self.assertTrue(index.known)
        self.assertEqual(index.cached_prefix_blocks(names), 0)
        self.assertTrue(index.changes_after(1)[1][0]["cleared"])

    def test_changes_report_net_presence_per_batch_with_duplicate_copies(self):
        """vLLM can hold one hash twice; a batch's change lists only net transitions."""
        index = ResidencyIndex(MODEL, TOKENIZER)
        prompt = tuple(range(12))
        names = identities(tuple(range(16)))
        removed = {"type": "BlockRemoved", "group_idx": 0}
        self.apply(index, 0, stored([1, 2, 3], prompt))
        # Block 4 arrives and leaves within one batch.
        self.apply(
            index, 1, stored([4], (12, 13, 14, 15), parent=3), {**removed, "block_hashes": [4]}
        )
        delta = index.changes_after(0)[1][0]["groups"]["0"]
        self.assertEqual((delta["stored"], delta["removed"]), ([], []))
        # A second copy of block 3 arrives and one copy leaves.
        self.apply(index, 2, stored([3], prompt[8:], parent=2), {**removed, "block_hashes": [3]})
        self.assertEqual(index.cached_prefix_blocks(names), 3)
        self.assertEqual(index.changes_after(1)[1][0]["groups"]["0"]["removed"], [])
        self.apply(index, 3, {**removed, "block_hashes": [3]})
        self.assertEqual(index.cached_prefix_blocks(names), 2)
        self.assertEqual(index.changes_after(2)[1][0]["groups"]["0"]["removed"], [names[2].hex()])

    def test_change_log_bound_and_mixed_block_sizes(self):
        index = ResidencyIndex(MODEL, TOKENIZER, max_change_blocks=3)
        prompt = tuple(range(8))
        self.apply(index, 0, stored([1, 2], prompt))
        self.apply(index, 1, {"type": "BlockRemoved", "block_hashes": [2], "group_idx": 0})
        self.assertIsNotNone(index.changes_after(-1))
        self.apply(index, 2, stored([2], prompt[4:], parent=1))
        # The log holds at most three changed blocks, so batch 0 leaves it.
        self.assertIsNone(index.changes_after(-1))
        self.assertEqual(len(index.changes_after(0)[1]), 2)
        # The batch-count bound applies with the block bound.
        bounded = ResidencyIndex(MODEL, TOKENIZER, max_changes=2, max_change_blocks=3)
        for sequence in range(4):
            self.apply(
                bounded, sequence, stored([sequence + 1], range(4 * sequence, 4 * sequence + 4))
            )
        self.assertIsNone(bounded.changes_after(0))
        self.assertEqual(len(bounded.changes_after(1)[1]), 2)
        self.apply(index, 3, stored([9], prompt, group=1, size=8))
        self.assertFalse(index.known)
        self.assertIn("block size 8", index.snapshot()["reason"])

    def test_a_lost_feed_reports_its_reason_during_replay(self):
        index = ResidencyIndex(MODEL, TOKENIZER)
        index.set_current(False)
        index.lose("cache-event subscription failed: ZMQError")
        self.assertIn("subscription failed", index.snapshot()["reason"])

    def test_missing_history_stays_unknown_until_a_cache_reset(self):
        """Late starts, gaps and unreadable batches serve no blocks."""
        prompt = tuple(range(4))
        cases = {
            "late": [(3, batch(stored([1], prompt)))],
            "gap": [(0, batch(stored([1], prompt))), (2, batch())],
            "unreadable": [(0, b"\xc1")],
            "unknown type": [(0, batch({"type": "FutureEvent"}))],
        }
        for name, batches in cases.items():
            with self.subTest(name):
                index = ResidencyIndex(MODEL, TOKENIZER)
                for sequence, payload in batches:
                    try:
                        events = decode_batch(payload)
                    except ValueError:
                        events = None
                    index.apply(sequence, events)
                self.assertFalse(index.known)
                self.assertEqual(index.snapshot()["groups"], [])
                self.assertIsNone(index.changes_after(0))
                last = index.sequence
                self.apply(index, last + 1, stored([2], prompt))
                self.assertEqual(index.cached_prefix_blocks(identities(prompt)), 0)
                self.apply(index, last + 2, {"type": "AllBlocksCleared"})
                self.apply(index, last + 3, stored([2], prompt))
                self.assertTrue(index.known)
                self.assertEqual(index.cached_prefix_blocks(identities(prompt)), 1)

    def test_sliding_window_groups_need_only_the_trailing_window(self):
        """vLLM reuses a sliding-window prefix when blocks cover the window before its end."""
        prompt = tuple(range(24))
        names = identities(prompt)
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.apply(
            index,
            0,
            stored([1, 2, 3, 4, 5, 6], prompt),
            stored(
                [1, 2, 3, 4, 5, 6],
                prompt,
                group=1,
                kind="sliding_window",
                kv_cache_spec_sliding_window=5,
            ),
        )
        # The window frees early blocks; a 5-token window needs one block before each end.
        self.apply(index, 1, {"type": "BlockRemoved", "block_hashes": [1, 2], "group_idx": 1})
        self.assertEqual(index.cached_prefix_blocks(names), 6)
        self.apply(index, 2, {"type": "BlockRemoved", "block_hashes": [6], "group_idx": 1})
        self.assertEqual(index.cached_prefix_blocks(names), 5)
        unsupported = ResidencyIndex(MODEL, TOKENIZER)
        self.apply(unsupported, 0, stored([1], prompt[:4], kind="chunked_local_attention"))
        self.assertEqual(unsupported.cached_prefix_blocks(names), 0)
        windowless = ResidencyIndex(MODEL, TOKENIZER)
        self.apply(windowless, 0, stored([1], prompt[:4], kind="sliding_window"))
        self.assertEqual(windowless.cached_prefix_blocks(names), 0)

    def test_duplicate_and_out_of_order_batches_change_nothing(self):
        """A replayed or late batch whose sequence was already applied is ignored."""
        prompt = tuple(range(8))
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.apply(index, 0, stored([1, 2], prompt))
        self.apply(index, 1, {"type": "BlockRemoved", "block_hashes": [2], "group_idx": 0})
        # Replayed batch 0 and a late batch 1.
        self.apply(index, 0, stored([1, 2], prompt))
        self.apply(index, 1, {"type": "BlockRemoved", "block_hashes": [1], "group_idx": 0})
        self.assertTrue(index.known)
        self.assertEqual(index.sequence, 1)
        self.assertEqual(index.cached_prefix_blocks(identities(prompt)), 1)
        # A duplicate store inside one batch leaves one resident block.
        self.apply(
            index,
            2,
            stored([3], tuple(range(4, 8)), parent=1),
            stored([3], tuple(range(4, 8)), parent=1),
        )
        self.assertEqual(index.cached_prefix_blocks(identities(prompt)), 2)
        self.assertEqual(len(index.snapshot()["groups"][0]["identities"]), 2)

    def test_bound_offload_tiers_and_unnamed_blocks(self):
        index = ResidencyIndex(MODEL, TOKENIZER, max_blocks=2)
        prompt = tuple(range(12))
        self.apply(index, 0, stored([1], prompt[:4], medium="CPU"))
        self.assertEqual(index.snapshot()["groups"], [])
        # A continuation whose parent the index never saw is resident but unnamed.
        self.apply(index, 1, stored([9], prompt[4:8], parent=8))
        self.assertEqual(index.snapshot()["groups"][0]["unnamed"], 1)
        self.apply(index, 2, stored([1, 2], prompt[:8]))
        self.assertFalse(index.known)
        self.assertIn("exceeds 2", index.reason)

    def test_empty_replay_is_a_known_empty_cache(self):
        index = ResidencyIndex(MODEL, TOKENIZER)
        index.mark_empty()
        self.assertTrue(index.known)
        self.apply(index, 0, stored([1], range(4)))
        self.assertEqual(index.cached_prefix_blocks(identities(tuple(range(4)))), 1)


class ResidencyFeedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)

    def run_feed(self, publisher, index):
        feed = ResidencyFeed(
            index, publisher.endpoint, publisher.replay_endpoint, replay_timeout_s=1, poll_s=0.02
        )
        feed.start()
        self.addCleanup(feed.stop)
        return feed

    def test_replay_rebuilds_history_and_live_batches_follow(self):
        publisher = FakePublisher(self.directory)
        self.addCleanup(publisher.close)
        prompt = tuple(range(8))
        publisher.publish(batch(stored([1], prompt[:4])))
        publisher.publish(batch(stored([2], prompt[4:], parent=1)))
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(publisher, index)
        self.assertTrue(wait_for(lambda: index.sequence == 1))
        self.assertEqual(index.cached_prefix_blocks(identities(prompt)), 2)
        publisher.publish(batch())
        self.assertTrue(wait_for(lambda: index.sequence == 2))
        # A batch lost on the live socket is recovered from replay.
        last = publisher.sequence
        publisher.publish(batch({"type": "BlockRemoved", "block_hashes": [2]}), deliver=False)
        publisher.publish(batch())
        self.assertTrue(wait_for(lambda: index.sequence == last + 1))
        self.assertTrue(index.known)
        self.assertEqual(index.cached_prefix_blocks(identities(prompt)), 1)

    def test_a_long_replay_completes_while_batches_keep_arriving(self):
        """The replay timeout bounds silence between batches, not the whole history."""
        publisher = FakePublisher(self.directory, replay_gap_s=0.3)
        self.addCleanup(publisher.close)
        for _ in range(6):
            publisher.publish(batch())
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(publisher, index)
        self.assertTrue(wait_for(lambda: index.sequence == 5, timeout=6))
        self.assertTrue(wait_for(lambda: index.snapshot()["known"]))

    def test_residency_stays_unknown_until_replay_rounds_reach_the_stream(self):
        """Between replay rounds the index is consistent but stale, so it serves nothing."""
        publisher = FakePublisher(self.directory, replay_limit=2, replay_gap_s=0.1)
        self.addCleanup(publisher.close)
        for _ in range(8):
            publisher.publish(batch())
        index = ResidencyIndex(MODEL, TOKENIZER)
        feed = ResidencyFeed(
            index, publisher.endpoint, publisher.replay_endpoint, replay_timeout_s=0.3, poll_s=0.02
        )
        feed.start()
        self.addCleanup(feed.stop)
        self.assertTrue(wait_for(lambda: index.sequence is not None and index.sequence >= 1))
        snapshot = index.snapshot()
        self.assertEqual(
            (snapshot["known"], snapshot["reason"]), (False, "replaying buffered history")
        )
        self.assertIsNone(index.changes_after(0))
        self.assertTrue(wait_for(lambda: index.snapshot()["known"], timeout=8))
        self.assertEqual(index.sequence, 7)

    def test_truncated_replays_continue_from_the_first_missing_batch(self):
        """A replay that loses its tail and end marker resumes until the history is complete."""
        publisher = FakePublisher(self.directory, replay_limit=4)
        self.addCleanup(publisher.close)
        prompt = tuple(range(4))
        publisher.publish(batch(stored([1], prompt)))
        for _ in range(12):
            publisher.publish(batch())
        index = ResidencyIndex(MODEL, TOKENIZER)
        feed = ResidencyFeed(
            index, publisher.endpoint, publisher.replay_endpoint, replay_timeout_s=0.3, poll_s=0.02
        )
        feed.start()
        self.addCleanup(feed.stop)
        self.assertTrue(wait_for(lambda: index.sequence == 12, timeout=8))
        self.assertTrue(index.known)
        self.assertEqual(index.cached_prefix_blocks(identities(prompt)), 1)

    def test_history_outside_the_replay_buffer_waits_for_a_reset(self):
        publisher = FakePublisher(self.directory, buffer=2)
        self.addCleanup(publisher.close)
        for _ in range(3):
            publisher.publish(batch(stored([1], range(4))))
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(publisher, index)
        self.assertTrue(wait_for(lambda: index.sequence == 2))
        self.assertFalse(index.known)
        cleared = batch({"type": "AllBlocksCleared"})
        # Repeat the reset until the live subscription has joined.
        self.assertTrue(wait_for(lambda: publisher.publish(cleared) or index.known))

    def test_malformed_messages_leave_residency_unknown(self):
        """A feed that cannot read its socket stops serving a stale known state."""
        publisher = FakePublisher(self.directory)
        self.addCleanup(publisher.close)
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(publisher, index)
        self.assertTrue(wait_for(lambda: index.known))
        with self.assertLogs("narwhal.residency", level="ERROR"):
            self.assertTrue(
                wait_for(lambda: publisher.pub.send_multipart([b"", b"short"]) or not index.known)
            )
        self.assertIn("cache-event subscription failed", index.reason)

    def test_idle_engine_is_known_empty_and_missing_replay_is_unknown(self):
        publisher = FakePublisher(self.directory)
        self.addCleanup(publisher.close)
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(publisher, index)
        self.assertTrue(wait_for(lambda: index.known))
        self.assertEqual(index.snapshot()["sequence"], -1)
        other = self.directory / "no-replay"
        other.mkdir()
        silent = FakePublisher(other, replay=False)
        self.addCleanup(silent.close)
        blind = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(silent, blind)
        self.assertTrue(wait_for(lambda: "replay is unavailable" in blind.reason))
        self.assertFalse(blind.known)


class CachedPrefixBlockWindowTests(unittest.TestCase):
    def test_window_groups_match_a_full_rescan(self):
        rng = random.Random(7)
        ids = [bytes([n]) * 32 for n in range(24)]

        def rescan(full, window, needed, boundary):
            best = 0
            for count, identity in enumerate(ids, start=1):
                if identity not in full:
                    break
                if identity in boundary and all(
                    i in window for i in ids[max(0, count - needed) : count]
                ):
                    best = count
            return best

        for _ in range(400):
            full = set(ids[: rng.randint(0, 24)])
            window = {i for i in ids if rng.random() < 0.8}
            boundary = {i for i in ids if rng.random() < 0.5}
            window_tokens = rng.randint(2, 40)
            needed = -(-(window_tokens - 1) // 4)
            groups = [
                ("full_attention", None, full),
                ("sliding_window", window_tokens, window),
                ("mamba", None, boundary),
            ]
            self.assertEqual(
                cached_prefix_blocks(groups, ids, 4), rescan(full, window, needed, boundary)
            )
