"""Prometheus text exposition lines and SLO-scaled histograms."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# Histogram bucket edges as fractions of the SLO.
_SLO_FRACTIONS = (0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 1.0, 1.5, 3.0, 10.0)


def buckets_for(slo_s: float) -> tuple[float, ...]:
    """Scale histogram edges to an SLO in seconds."""
    return tuple(round(f * slo_s, 6) for f in _SLO_FRACTIONS)


def slo_label(slo_s: float) -> str:
    """Label value naming the target that scaled a histogram's edges."""
    return f"{slo_s:g}"


@dataclass
class Histogram:
    """Prometheus histogram with fixed bucket edges."""

    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    n: int = 0
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        """Add `value` to the bucket counts, sum and count."""
        self.total += value
        self.n += 1
        for i, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[i] += 1

    def render(self, name: str, help_text: str) -> list[str]:
        """Render the histogram's Prometheus exposition lines."""
        return [f"# HELP {name} {help_text}", f"# TYPE {name} histogram", *self.samples(name)]

    def samples(self, name: str) -> list[str]:
        """Render this series' bucket, sum and count lines without the family header."""
        out: list[str] = []
        prefix = "".join(f'{k}="{v}",' for k, v in self.labels.items())
        series = "{" + prefix[:-1] + "}" if prefix else ""
        for edge, count in zip(self.buckets, self.counts, strict=True):
            out.append(f'{name}_bucket{{{prefix}le="{edge}"}} {count}')
        out.append(f'{name}_bucket{{{prefix}le="+Inf"}} {self.n}')
        out.append(f"{name}_sum{series} {self.total}")
        out.append(f"{name}_count{series} {self.n}")
        return out


def render_histograms(name: str, help_text: str, series: Sequence[Histogram]) -> list[str]:
    """Render several labelled series of one histogram family."""
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
    for histogram in series:
        out += histogram.samples(name)
    return out


def slo_histogram(slo_s: float) -> Histogram:
    """Histogram whose edges scale with `slo_s`, labelled with that target."""
    return Histogram(buckets_for(slo_s), labels={"slo": slo_label(slo_s)})


def metric_lines(
    name: str,
    help_text: str,
    kind: str,
    samples: Sequence[tuple[Mapping[str, str], float | int]],
) -> list[str]:
    """Return one metric family's HELP, TYPE and sample lines."""
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        label_s = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else ""
        out.append(f"{name}{label_s} {value}")
    return out
