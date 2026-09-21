"""Check transport classification and exact router-wide retry credits."""

import unittest

import httpx

from narwhal.engines.client import EngineError
from narwhal.engines.connector import HandoffExpired
from narwhal.serving.retry import RetryBudget, RetryPolicy, leg_failure_reason, transient


class RetryTests(unittest.TestCase):
    """Each classification has a retry decision and a quarantine reason."""

    def test_engine_status_classification(self):
        """Each HTTP status selects a retry decision and engine failure reason."""
        for status, retry, reason in (
            (400, False, None),
            (401, False, None),
            (404, False, None),
            (408, True, "overloaded"),
            (429, True, "overloaded"),
            (500, True, "engine_error"),
            (502, True, "engine_error"),
            (503, True, "engine_error"),
            (504, True, "engine_error"),
            (501, False, "engine_error"),
        ):
            with self.subTest(status=status):
                exc = EngineError("decode", "http://engine", status, "failure")
                self.assertEqual(transient(exc), retry)
                self.assertEqual(leg_failure_reason(exc), reason)

    def test_transport_classification(self):
        """Pool saturation stays local while network faults identify the engine."""
        for exc, retry, reason in (
            (httpx.PoolTimeout("pool"), False, "local_pool"),
            (httpx.ConnectTimeout("connect"), True, "engine_dead"),
            (httpx.ConnectError("connect"), True, "engine_dead"),
            (httpx.ReadTimeout("read"), True, "timeout"),
            (httpx.ReadError("read"), True, "engine_dead"),
            (httpx.RemoteProtocolError("protocol"), True, "engine_dead"),
            (TimeoutError(), False, "timeout"),
            (ValueError("bad input"), False, "engine_dead"),
            (HandoffExpired(), True, "engine_dead"),
        ):
            with self.subTest(kind=type(exc).__name__):
                self.assertEqual(transient(exc), retry)
                self.assertEqual(leg_failure_reason(exc), reason)

    def test_jitter_scales_and_caps_each_attempt(self):
        """Injected random endpoints bound the exponentially increasing delay."""
        policy = RetryPolicy(base_delay_s=0.1, max_delay_s=0.3)
        for attempt, ceiling in ((1, 0.1), (2, 0.2), (3, 0.3), (10, 0.3)):
            with self.subTest(attempt=attempt):
                self.assertEqual(policy.delay(attempt, lambda: 0), 0)
                self.assertEqual(policy.delay(attempt, lambda: 1), ceiling)
                self.assertEqual(policy.delay(attempt, lambda: 0.5), ceiling / 2)

    def test_fractional_replenishment_crosses_exact_credit_boundary(self):
        """Ten tenths buy exactly one retry after the initial credit is spent."""
        budget = RetryBudget(1, 0.1)
        self.assertTrue(budget.acquire())
        for _ in range(9):
            budget.succeeded()
        self.assertEqual(budget.available, 0.9)
        self.assertFalse(budget.acquire())
        budget.succeeded()
        self.assertTrue(budget.acquire())
        self.assertEqual((budget.available, budget.spent, budget.denied), (0, 2, 1))

    def test_refills_stop_at_capacity_and_zero_capacity_stays_empty(self):
        """Successful requests preserve the configured bucket ceiling."""
        for capacity in (0, 2):
            with self.subTest(capacity=capacity):
                budget = RetryBudget(capacity, 0.5)
                for _ in range(10):
                    budget.succeeded()
                self.assertEqual(budget.available, capacity)
                for _ in range(capacity):
                    self.assertTrue(budget.acquire())
                self.assertFalse(budget.acquire())
