"""Queue, retry and stream-continuation limits for one router process."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .retry import RetryPolicy


@dataclass(frozen=True)
class ContinuationPolicy:
    """Require explicit capacity and qualification before enabling continuation."""

    enabled: bool = False
    # Recovery attempts exclude the original prefill/decode attempt.
    max_attempts: int = 0
    recovery_budget: int = 0
    recovery_replenish: float = 0.0
    # The original prompt plus requested output must fit this token ceiling.
    max_context_tokens: int = 0
    max_history_bytes: int = 0
    max_retained_bytes: int = 0
    qualification_path: str = ""
    qualification_sha256: str = ""

    def validate(self) -> None:
        """Validate policy values without opening the private qualification record."""
        if type(self.enabled) is not bool:
            raise ValueError("continuation.enabled must be a boolean")
        counts = (
            "max_attempts",
            "recovery_budget",
            "max_context_tokens",
            "max_history_bytes",
            "max_retained_bytes",
        )
        for name in counts:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"continuation.{name} must be a nonnegative integer")
        value = self.recovery_replenish
        if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
            raise ValueError("continuation.recovery_replenish must be finite and between 0 and 1")
        if not isinstance(self.qualification_path, str) or "\x00" in self.qualification_path:
            raise ValueError("continuation.qualification_path must be a path string")
        if not isinstance(self.qualification_sha256, str) or (
            self.qualification_sha256
            and re.fullmatch(r"[0-9a-f]{64}", self.qualification_sha256) is None
        ):
            raise ValueError(
                "continuation.qualification_sha256 must be empty or 64 lowercase hexadecimal digits"
            )
        if not self.enabled:
            return
        for name in counts:
            if getattr(self, name) < 1:
                raise ValueError(f"continuation.{name} must be positive when enabled")
        if self.max_context_tokens < 2:
            raise ValueError("continuation.max_context_tokens must be at least 2 when enabled")
        if self.max_history_bytes > self.max_retained_bytes:
            raise ValueError(
                "continuation.max_history_bytes must not exceed continuation.max_retained_bytes"
            )
        if not self.qualification_path.strip():
            raise ValueError("continuation.qualification_path is required when enabled")
        if not self.qualification_sha256:
            raise ValueError("continuation.qualification_sha256 is required when enabled")


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
