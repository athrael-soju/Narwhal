"""Exercise retained token frontiers independently of backend replay qualification."""

import json
import sys
import unittest

from narwhal.engines.replay import ReplayEvent
from narwhal.serving.continuation import (
    CommitGroup,
    ContinuationHistory,
    HistoryBudget,
    HistoryCapacityError,
    HistoryLimitExceeded,
)


def completion(ids=(), text="", finish=None):
    return ReplayEvent(
        kind="completion",
        envelope={},
        generated_ids=tuple(ids),
        text=text,
        finish_reason=finish,
    )


def wire(event):
    if event.kind == "done":
        return b"data: [DONE]\n\n"
    obj = {"choices": []}
    if event.kind == "completion":
        obj["choices"] = [
            {
                "index": 0,
                "text": event.text,
                "token_ids": list(event.generated_ids),
                "finish_reason": event.finish_reason,
            }
        ]
    return ("data: " + json.dumps(obj) + "\n\n").encode()


class ContinuationHistoryTests(unittest.TestCase):
    def make_history(self, prompt=(101, 102), max_tokens=8, quota=16384):
        budget = HistoryBudget(quota)
        reservation = budget.reserve(quota)
        self.assertIsNotNone(reservation)
        history = ContinuationHistory.create(prompt, max_tokens, reservation)
        self.addCleanup(history.close)
        return budget, history

    def append(self, history, event, at=1.0):
        return history.append(event, wire(event), at)

    def test_budget_reserves_whole_requests_and_returns_capacity_once(self):
        budget = HistoryBudget(200)
        first = budget.reserve(100)
        second = budget.reserve(100)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(budget.reserve(1))
        self.assertEqual((budget.used, budget.high_water), (200, 200))
        first.close()
        first.close()
        self.assertEqual(budget.used, 100)
        third = budget.reserve(100)
        self.assertIsNotNone(third)
        second.close()
        third.close()
        self.assertEqual((budget.used, budget.high_water), (0, 200))
        other = HistoryBudget(100)
        self.assertEqual(other.used, 0)

    def test_invalid_quota_and_invalid_prompt_release_owned_capacity(self):
        for quota in (True, 0, -1, 1.5):
            with self.subTest(quota=quota), self.assertRaises(HistoryCapacityError):
                HistoryBudget(16384).reserve(quota)
        for prompt, max_tokens, quota in (
            ((), 1, 16384),
            ((True,), 1, 16384),
            ((-1,), 1, 16384),
            ((1 << 64,), 1, 16384),
            ((1,), True, 16384),
            ((1,), 0, 16384),
            ((1,), 1, 100),
            ((1,), 10**30, 16384),
        ):
            with self.subTest(prompt=prompt, max_tokens=max_tokens, quota=quota):
                budget = HistoryBudget(quota)
                reservation = budget.reserve(quota)
                with self.assertRaises(HistoryCapacityError):
                    ContinuationHistory.create(prompt, max_tokens, reservation)
                self.assertEqual(budget.used, 0)
                reservation.close()
                self.assertEqual(budget.used, 0)

    def test_reservation_cannot_back_two_histories(self):
        budget, history = self.make_history()
        with self.assertRaises(HistoryCapacityError):
            ContinuationHistory.create([1], 2, history.reservation)
        self.assertEqual(budget.used, history.reservation.bytes)
        self.assertEqual(history.replay_prompt(), [101, 102])

    def test_empty_and_grouped_tokens_wait_for_a_singleton_boundary(self):
        _, history = self.make_history()
        events = [
            completion([11], ""),
            completion([12, 13], "visible prefix"),
            completion([14], "🦄"),
        ]
        self.assertIsNone(self.append(history, events[0]))
        self.assertIsNone(self.append(history, events[1], at=2.0))
        group = self.append(history, events[2], at=3.0)
        self.assertIsInstance(group, CommitGroup)
        self.assertEqual(group.data, b"".join(wire(event) for event in events))
        self.assertEqual(group.frontier, 4)
        self.assertEqual((history.observed_count, history.committed_count), (4, 0))
        self.assertEqual(history.replay_prompt(), [101, 102])
        self.assertEqual(history.remaining, 8)
        history.commit(group, 4.0)
        self.assertEqual(history.replay_prompt(), [101, 102, 11, 12, 13, 14])
        self.assertEqual((history.committed_count, history.remaining), (4, 4))
        self.assertEqual((history.pending_size, history.inflight), (0, None))
        self.assertEqual((history.first_observed_at, history.last_observed_at), (1.0, 3.0))
        self.assertEqual((history.first_committed_at, history.last_committed_at), (4.0, 4.0))

    def test_pending_send_keeps_ids_uncommitted_and_prevents_read_ahead(self):
        budget, history = self.make_history()
        group = self.append(history, completion([11], "x"))
        self.assertEqual(history.committed_count, 0)
        self.assertEqual(budget.used, history.reservation.bytes)
        with self.assertRaisesRegex(RuntimeError, "awaits acknowledgement"):
            self.append(history, completion([12], "y"))
        self.assertEqual(history.inflight, group)
        history.close()
        history.close()
        self.assertEqual((history.committed_count, budget.used), (0, 0))
        with self.assertRaisesRegex(RuntimeError, "closed"):
            history.commit(group, 2.0)

    def test_stale_or_copied_acknowledgements_cannot_advance_frontier(self):
        _, history = self.make_history()
        old = self.append(history, completion([11], "x"))
        copied = CommitGroup(old.data, old.frontier, old.terminal, old.sequence)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            history.commit(copied, 2.0)
        history.discard_pending()
        current = self.append(history, completion([12], "y"), at=3.0)
        self.assertGreater(current.sequence, old.sequence)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            history.commit(old, 4.0)
        self.assertEqual(history.committed_count, 0)
        history.commit(current, 4.0)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            history.commit(current, 5.0)
        self.assertEqual(history.replay_prompt(), [101, 102, 12])

    def test_finish_and_usage_commit_only_after_done(self):
        _, history = self.make_history(max_tokens=2)
        first = self.append(history, completion([11], "x"))
        history.commit(first, 2.0)
        finish = completion([12], "", "stop")
        usage = ReplayEvent(kind="usage", envelope={})
        done = ReplayEvent(kind="done", envelope={})
        self.assertIsNone(self.append(history, finish, at=3.0))
        self.assertIsNone(self.append(history, usage, at=4.0))
        self.assertEqual((history.committed_count, history.remaining), (1, 1))
        group = self.append(history, done, at=5.0)
        self.assertEqual(group.data, wire(finish) + wire(usage) + wire(done))
        self.assertFalse(history.terminal_committed)
        history.commit(group, 6.0)
        self.assertTrue(history.terminal_committed)
        self.assertEqual((history.committed_count, history.remaining), (2, 0))
        self.assertEqual(history.first_committed_at, 2.0)
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            history.discard_pending()

    def test_lost_terminal_group_discards_only_uncommitted_tokens(self):
        _, history = self.make_history(max_tokens=3)
        first = self.append(history, completion([11], "x"), at=1.0)
        history.commit(first, 2.0)
        self.append(history, completion([12], "", "stop"), at=3.0)
        history.discard_pending()
        self.assertEqual(history.replay_prompt(), [101, 102, 11])
        self.assertEqual((history.observed_count, history.committed_count), (1, 1))
        resumed = self.append(history, completion([13], "y"), at=4.0)
        history.commit(resumed, 5.0)
        self.assertEqual(history.replay_prompt(), [101, 102, 11, 13])
        self.assertEqual((history.first_observed_at, history.first_committed_at), (1.0, 2.0))
        self.assertEqual((history.last_observed_at, history.last_committed_at), (4.0, 5.0))
        self.assertEqual((history.max_tokens, history.remaining), (3, 1))

    def test_wire_overflow_has_no_partial_append_or_commit(self):
        _, history = self.make_history()
        event = completion([11], "")
        self.assertIsNone(history.append(event, b"x" * history.pending_capacity, 1.0))
        self.assertEqual(history.pending_size, history.pending_capacity)
        with self.assertRaisesRegex(HistoryLimitExceeded, "byte limit"):
            self.append(history, completion([12], "y"), at=2.0)
        self.assertEqual(history.observed_count, 1)
        self.assertEqual(history.committed_count, 0)
        self.assertEqual(history.pending_size, history.pending_capacity)

    def test_token_overflow_and_invalid_ids_do_not_mutate_history(self):
        _, history = self.make_history(max_tokens=2)
        self.append(history, completion([11, 12], "x"))
        for ids, exception in (
            ([13], HistoryLimitExceeded),
            ([True], ValueError),
            ([-1], ValueError),
            ([1 << 64], ValueError),
        ):
            with self.subTest(ids=ids), self.assertRaises(exception):
                self.append(history, completion(ids, "y"))
            self.assertEqual((history.observed_count, history.committed_count), (2, 0))
            self.assertEqual(history.replay_prompt(), [101, 102])

    def test_done_without_finish_and_completion_after_finish_are_rejected(self):
        _, history = self.make_history()
        with self.assertRaisesRegex(ValueError, "finished choice"):
            self.append(history, ReplayEvent(kind="done", envelope={}))
        self.append(history, completion([11], "x", "length"))
        with self.assertRaisesRegex(ValueError, "follows a finish"):
            self.append(history, completion([12], "y"))
        self.assertEqual((history.observed_count, history.committed_count), (1, 0))

    def test_buffers_and_replay_copy_fit_reserved_storage_without_growth(self):
        _, history = self.make_history(prompt=(0, (1 << 64) - 1), max_tokens=8)
        ids_size = sys.getsizeof(history._ids)
        pending_size = sys.getsizeof(history._pending)
        for index in range(8):
            incoming = b"x" * history.pending_capacity
            group = history.append(completion([(1 << 64) - 1], "x"), incoming, float(index))
            history.commit(group, float(index))
            replay = history.replay_prompt()
            charged = (
                sys.getsizeof(history)
                + sys.getsizeof(history.reservation)
                + sys.getsizeof(history._ids)
                + sys.getsizeof(history._pending)
                + sys.getsizeof(group)
                + sys.getsizeof(group.data)
                + sys.getsizeof(incoming)
                + sys.getsizeof(replay)
                + sum(sys.getsizeof(token) for token in replay)
            )
            self.assertLessEqual(charged, history.reservation.bytes)
            self.assertEqual(sys.getsizeof(history._ids), ids_size)
            self.assertEqual(sys.getsizeof(history._pending), pending_size)
        self.assertEqual(history.remaining, 0)
        self.assertEqual(len(replay), 10)

    def test_errors_and_representations_do_not_include_content(self):
        _, history = self.make_history(prompt=(98765432123456789,), max_tokens=2)
        private_text = "retained output must stay out of diagnostics"
        group = self.append(history, completion([8765432123456789], private_text))
        for value in (history, history.reservation, group):
            self.assertNotIn(private_text, repr(value))
            self.assertNotIn("98765432123456789", repr(value))
            self.assertNotIn("8765432123456789", repr(value))
        with self.assertRaises(RuntimeError) as caught:
            self.append(history, completion([1], private_text))
        self.assertNotIn(private_text, str(caught.exception))
