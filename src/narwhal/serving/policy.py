"""Explicit queue capacity and bounded retry settings for one router process."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .retry import RetryPolicy


@dataclass(frozen=True)
class ServingPolicy:
    """Keep the queue-free, single-attempt baseline until limits are configured."""

    queue_capacity: int = 0
    queue_timeout_s: float = 0.0
    prefill_concurrency: int = 0
    decode_concurrency: int = 0
    # Conservative local age bound, shorter than the verified producer KV lease.
    handoff_timeout_s: float = 0.0
    max_attempts: int = 1
    retry_base_s: float = 0.1
    retry_cap_s: float = 1.0
    retry_budget: int = 10
    retry_replenish: float = 0.1
    # Byte ceilings bound retained bodies independently of token estimates.
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 16 * 1024 * 1024

    def validate(self) -> None:
        """Reject unbounded, mistyped, and contradictory serving settings."""
        for name in (
            "queue_capacity",
            "prefill_concurrency",
            "decode_concurrency",
            "retry_budget",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"serving.{name} must be a nonnegative integer")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("serving.max_attempts must be an integer between 1 and 3")
        for name in ("max_request_bytes", "max_response_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"serving.{name} must be a positive integer")
        for name in (
            "queue_timeout_s",
            "handoff_timeout_s",
            "retry_base_s",
            "retry_cap_s",
            "retry_replenish",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"serving.{name} must be finite and nonnegative")
        if self.queue_capacity and (
            self.queue_timeout_s <= 0 or self.prefill_concurrency < 1 or self.decode_concurrency < 1
        ):
            raise ValueError(
                "serving.queue_capacity requires positive queue_timeout_s, "
                "prefill_concurrency, and decode_concurrency from workload measurements"
            )
        if self.retry_base_s <= 0 or self.retry_cap_s < self.retry_base_s:
            raise ValueError("serving retry delays require 0 < retry_base_s <= retry_cap_s")
        if self.retry_replenish > 1:
            raise ValueError("serving.retry_replenish must not exceed one credit per completion")
        if (self.queue_capacity or self.max_attempts > 1) and self.handoff_timeout_s <= 0:
            raise ValueError(
                "queued or retried serving requires positive serving.handoff_timeout_s "
                "below the verified backend KV lease"
            )

    def retry_policy(self) -> RetryPolicy:
        """Return the per-request attempt and delay policy."""
        return RetryPolicy(self.max_attempts, self.retry_base_s, self.retry_cap_s)
