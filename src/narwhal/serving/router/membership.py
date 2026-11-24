"""The router's engine membership: one immutable snapshot per generation."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ...config import EngineSpec


@dataclass(frozen=True)
class Membership:
    """The engines the router serves, with a generation that only increases.

    The router process reads membership only from its current snapshot. A change replaces the
    snapshot with a successor whose generation is one higher. Treat the specs as read-only.
    """

    generation: int
    specs: tuple[EngineSpec, ...]
    _by_id: dict[str, EngineSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("membership generation must be nonnegative")
        by_id: dict[str, EngineSpec] = {}
        for spec in self.specs:
            if spec.iid in by_id:
                raise ValueError(f"duplicate engine id {spec.iid!r}")
            by_id[spec.iid] = spec
        object.__setattr__(self, "_by_id", by_id)

    @classmethod
    def initial(cls, specs: Iterable[EngineSpec]) -> Membership:
        """Return generation 0 from the configured fleet."""
        return cls(0, tuple(specs))

    @property
    def ids(self) -> list[str]:
        """Engine IDs in snapshot order."""
        return [spec.iid for spec in self.specs]

    def spec(self, iid: str) -> EngineSpec:
        """Return the spec of a member; raises KeyError for a non-member."""
        return self._by_id[iid]

    def by_id(self) -> dict[str, EngineSpec]:
        """Return a new map from engine ID to spec."""
        return dict(self._by_id)

    def joined(self, spec: EngineSpec) -> Membership:
        """Return the next generation with `spec` added."""
        if spec.iid in self._by_id:
            raise ValueError(f"engine {spec.iid!r} is already a member")
        return Membership(self.generation + 1, (*self.specs, spec))

    def left(self, iid: str) -> Membership:
        """Return the next generation without `iid`."""
        if iid not in self._by_id:
            raise ValueError(f"engine {iid!r} is not a member")
        return Membership(self.generation + 1, tuple(s for s in self.specs if s.iid != iid))

    def __contains__(self, iid: object) -> bool:
        return iid in self._by_id

    def __iter__(self) -> Iterator[EngineSpec]:
        return iter(self.specs)

    def __len__(self) -> int:
        return len(self.specs)
