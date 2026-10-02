"""Gate outcome collection and printing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Report:
    """Print gate outcomes and collect failures and skips."""

    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pairs: list[dict[str, object]] = field(default_factory=list)
    calibration: dict[str, object] = field(default_factory=dict)

    def ok(self, msg: str) -> None:
        """Print a passing gate result."""
        print(f"  ok    {msg}")

    def fail(self, msg: str) -> None:
        """Print and record a failed gate result."""
        print(f"  FAIL  {msg}")
        self.failed.append(msg)

    def skip(self, msg: str) -> None:
        """Print and record a skipped gate result."""
        print(f"  SKIP  {msg}")
        self.skipped.append(msg)

    def warn(self, msg: str) -> None:
        """Report a configuration risk without changing gate outcomes."""
        print(f"  WARN  {msg}")
        self.warnings.append(msg)
