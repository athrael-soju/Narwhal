"""Redraw a launched instance's roles, load and latency from the router's state and metrics."""

from __future__ import annotations

import math
import re
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import httpx
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from . import lifecycle
from .ask import PHASES

FETCH_TIMEOUT_S = 5.0
# Latency and throughput cover the samples of this many seconds.
WINDOW_S = 30.0
RECENT_SWITCHES = 5
SERVED, TOKENS, FAILED = (
    "narwhal_served_total",
    "narwhal_output_tokens_total",
    "narwhal_failed_total",
)
# Requests the router turned away or timed out before an engine failed them.
UNSERVED = ("narwhal_refused_total", "narwhal_rejected_total", "narwhal_expired_total")
COUNTERS = (SERVED, TOKENS, FAILED, *UNSERVED)
# Frames of history behind each trend line.
TREND_FRAMES = 60
TICKS = "▁▂▃▄▅▆▇█"
ROLE_STYLES = {"prefill": "cyan", "decode": "magenta"}
STATE_STYLES = {"live": "green", "draining": "yellow", "ejected": "red", "quarantined": "red"}
_SAMPLE = re.compile(r"^(narwhal_[a-z_]+)(?:\{([^}]*)\})?\s+(\S+)$")
_LABEL = re.compile(r'(\w+)="([^"]*)"')
Fetch = Callable[[str], tuple[dict[str, Any], str]]


@dataclass(frozen=True)
class Sample:
    at: float
    state: dict[str, Any]
    # Cumulative bucket counts by upper bound, for each latency histogram.
    buckets: dict[str, dict[float, float]]
    counters: dict[str, float]


def parse(at: float, state: dict[str, Any], metrics: str) -> Sample:
    buckets: dict[str, dict[float, float]] = {"ttft": {}, "tpot": {}}
    counters: dict[str, float] = {}
    for line in metrics.splitlines():
        found = _SAMPLE.match(line)
        if found is None:
            continue
        name, labels, value = (
            found.group(1),
            dict(_LABEL.findall(found.group(2) or "")),
            found.group(3),
        )
        try:
            number = float(value)
        except ValueError:
            continue
        for key in buckets:
            if name == f"narwhal_{key}_seconds_bucket" and "le" in labels:
                bound = math.inf if labels["le"] == "+Inf" else float(labels["le"])
                buckets[key][bound] = buckets[key].get(bound, 0.0) + number
        if name in COUNTERS:
            counters[name] = counters.get(name, 0.0) + number
    return Sample(at, state, buckets, counters)


def quantile(counts: dict[float, float], fraction: float) -> float | None:
    """The quantile of a cumulative histogram, interpolated inside its bucket; None when empty.

    A quantile in the unbounded last bucket reports the highest finite bound.
    """
    bounds = sorted(counts)
    total = counts[bounds[-1]] if bounds else 0.0
    if total <= 0:
        return None
    rank = fraction * total
    lower, below = 0.0, 0.0
    for bound in bounds:
        if counts[bound] >= rank:
            if math.isinf(bound):
                return lower
            inside = counts[bound] - below
            return bound if inside <= 0 else lower + (bound - lower) * (rank - below) / inside
        lower, below = bound, counts[bound]
    return lower


def restarted(old: Sample, new: Sample) -> bool:
    """Whether the router's counters began again between two samples."""
    return any(new.counters.get(name, 0.0) < count for name, count in old.counters.items()) or any(
        set(new.buckets[key]) != set(counts)
        or any(new.buckets[key][bound] < count for bound, count in counts.items())
        for key, counts in old.buckets.items()
    )


def _spread(old: Sample | None, new: Sample, key: str) -> dict[str, float | None]:
    counts = {
        bound: count - (old.buckets[key].get(bound, 0.0) if old else 0.0)
        for bound, count in new.buckets[key].items()
    }
    return {"p50": quantile(counts, 0.5), "p95": quantile(counts, 0.95)}


def _rate(old: Sample | None, new: Sample, name: str) -> float | None:
    if old is None or new.at <= old.at:
        return None
    return max(0.0, new.counters.get(name, 0.0) - old.counters.get(name, 0.0)) / (new.at - old.at)


def view(old: Sample | None, new: Sample) -> dict[str, Any]:
    """What one frame shows: `new` against `old`, the oldest sample in the window."""
    state = new.state
    pools = state.get("pools") or {}
    roles = {iid: role for role, members in pools.items() for iid in members}
    resident = state.get("resident") or {}
    ejected, draining = set(state.get("ejected") or []), set(state.get("draining") or [])
    quarantined = set(state.get("quarantined") or [])
    engines = [
        {
            "iid": iid,
            "role": roles.get(iid),
            "requests": sum((resident.get(iid) or {}).values()),
            "state": "ejected"
            if iid in ejected
            else "quarantined"
            if iid in quarantined
            else "draining"
            if iid in draining
            else "live",
        }
        for iid in sorted(set(roles) | set(resident))
    ]
    flips = state.get("flips") or []
    # The flip list keeps a bounded history; the control counters keep the total.
    counted = (state.get("control") or {}).get("flips")
    switches = sum(counted.values()) if isinstance(counted, dict) else len(flips)
    return {
        "status": "observed",
        "engines": engines,
        "ttft_s": _spread(old, new, "ttft"),
        "tpot_s": _spread(old, new, "tpot"),
        "requests_per_second": _rate(old, new, SERVED),
        # Counted when a request completes, so a long reply lands in one frame.
        "output_tokens_per_second": _rate(old, new, TOKENS),
        "served": int(new.counters.get(SERVED, 0)),
        "failed": int(new.counters.get(FAILED, 0)),
        "unserved": int(sum(new.counters.get(name, 0) for name in UNSERVED)),
        "role_switches": switches,
        "recent_switches": [
            {"iid": flip.get("iid"), "to": flip.get("to")} for flip in flips[-RECENT_SWITCHES:]
        ],
        "window_s": None if old is None else new.at - old.at,
    }


def _seconds(value: float | None) -> str:
    return (
        "     -" if value is None else f"{value * 1000:5.0f}ms" if value < 1 else f"{value:5.2f}s "
    )


def _per_second(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def render(shown: dict[str, Any], router: str) -> str:
    counts = {
        role: sum(e["role"] == role for e in shown["engines"]) for role in ("prefill", "decode")
    }
    window = shown["window_s"]
    lines = [
        f"narwhal dev watch  {router}",
        f"{counts['prefill']} prefill, {counts['decode']} decode"
        + ("" if window is None else f"   figures cover the last {window:.0f} s"),
        "",
        "engine  role     requests  state",
        *(
            f"{e['iid']:<7} {e['role'] or '-':<8} {e['requests']:>8}  {e['state']}"
            for e in shown["engines"]
        ),
        "",
        "                       median    95th percentile",
        *(
            f"{label:<22} {_seconds(shown[key]['p50'])}    {_seconds(shown[key]['p95'])}"
            for label, key in (
                ("time to first token", "ttft_s"),
                ("time per output token", "tpot_s"),
            )
        ),
        "",
        f"throughput  {_per_second(shown['requests_per_second'])} requests/s   "
        f"{_per_second(shown['output_tokens_per_second'])} output tokens/s",
        f"served {shown['served']}   failed {shown['failed']}   "
        f"refused, rejected or expired {shown['unserved']}",
        f"role switches {shown['role_switches']}"
        + "".join(f"   {flip['iid']} to {flip['to']}" for flip in shown["recent_switches"]),
    ]
    return "\n".join(lines) + "\n"


def trend(values: list[float | None]) -> str:
    """One tick per frame between the lowest and highest value shown; a space where none was
    read, and a middle tick while every value is the same.
    """
    read = [value for value in values if value is not None]
    low, high = (min(read), max(read)) if read else (0.0, 0.0)
    middle = TICKS[len(TICKS) // 2 - 1]
    return "".join(
        " "
        if value is None
        else middle
        if high <= low
        else TICKS[int((value - low) / (high - low) * (len(TICKS) - 1) + 0.5)]
        for value in values
    )


@dataclass(frozen=True)
class Context:
    """What the terminal view shows beside a frame."""

    instance: Path
    router: str
    model: str | None
    slots: int | None


def _number(value: float | None, unit: str = "") -> str:
    return "-" if value is None else f"{value:,.1f}{unit}"


def panel(
    shown: dict[str, Any],
    context: Context,
    history: list[dict[str, Any]],
    switched: set[str] | None = None,
) -> Panel:
    """The terminal view of one frame; `history` holds the earlier frames, oldest first."""
    switched = switched or set()
    counts = {role: sum(e["role"] == role for e in shown["engines"]) for role in ROLE_STYLES}
    window = shown.get("window_s")
    heading = Text.assemble(
        (str(context.instance), "bold"),
        "   ",
        (context.router, "blue"),
        *(("   ", (context.model, "bold")) if context.model else ()),
    )
    split = Text.assemble(
        (f"{counts['prefill']} prefill", ROLE_STYLES["prefill"]),
        "  ",
        (f"{counts['decode']} decode", ROLE_STYLES["decode"]),
        ("" if not context.slots else f"   {context.slots} slots per engine", "dim"),
        ("" if window is None else f"   figures cover the last {window:.0f} s", "dim"),
    )
    # Bars share one scale, the larger of the slots and the busiest engine's requests.
    scale = max([context.slots or 1, *(engine["requests"] for engine in shown["engines"])])
    engines = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for name in ("engine", "role", "requests", "state"):
        engines.add_column(name)
    for engine in shown["engines"]:
        role = engine["role"] or "-"
        style = ROLE_STYLES.get(role, "dim")
        if engine["iid"] in switched:
            style = f"bold reverse {style}"
        load = Table.grid(padding=(0, 1))
        colour = ROLE_STYLES.get(role, "white")
        load.add_row(
            ProgressBar(
                total=scale,
                completed=engine["requests"],
                width=16,
                complete_style=colour,
                finished_style=colour,
            ),
            Text(str(engine["requests"]), justify="right"),
        )
        engines.add_row(
            engine["iid"],
            Text(role, style=style),
            load,
            Text(engine["state"], style=STATE_STYLES.get(engine["state"], "")),
        )
    frames = [*history, shown]
    figures = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    figures.add_column("")
    figures.add_column("median", justify="right")
    figures.add_column("95th percentile", justify="right")
    figures.add_column("trend", style="green")
    for label, key in (("time to first token", "ttft_s"), ("time per output token", "tpot_s")):
        figures.add_row(
            label,
            _seconds(shown[key]["p50"]).strip(),
            _seconds(shown[key]["p95"]).strip(),
            trend([frame[key]["p50"] for frame in frames]),
        )
    for label, key in (
        ("requests per second", "requests_per_second"),
        ("output tokens per second", "output_tokens_per_second"),
    ):
        figures.add_row(label, _number(shown[key]), "", trend([frame[key] for frame in frames]))
    totals = Text.assemble(
        "served ",
        (f"{shown['served']:,}", "bold green"),
        "   failed ",
        (f"{shown['failed']:,}", "bold red" if shown["failed"] else "bold"),
        "   refused, rejected or expired ",
        (f"{shown['unserved']:,}", "bold yellow" if shown["unserved"] else "bold"),
    )
    switches = Text.assemble("role switches ", (str(shown["role_switches"]), "bold"))
    for flip in shown["recent_switches"]:
        switches.append("   ")
        switches.append(f"{flip['iid']} → {flip['to']}", style=ROLE_STYLES.get(flip["to"], ""))
    return Panel(
        Group(heading, split, engines, figures, totals, switches),
        title="narwhal dev watch",
        subtitle="Ctrl+C to exit",
        border_style="blue",
    )


def _context(root: Path, config: dict[str, Any]) -> Context:
    try:
        spec = lifecycle.read(root / "template.json")
    except (OSError, ValueError):
        spec = {}
    return Context(
        instance=root,
        router=config["router_url"],
        model=(spec.get("model") or {}).get("served_name"),
        slots=(spec.get("runtime") or {}).get("slots"),
    )


def http_fetch(url: str) -> tuple[dict[str, Any], str]:
    with httpx.Client(timeout=FETCH_TIMEOUT_S, trust_env=False) as client:
        state = client.get(url + "/narwhal/state")
        state.raise_for_status()
        metrics = client.get(url + "/metrics")
        metrics.raise_for_status()
        return state.json(), metrics.text


def run(
    root: Path,
    *,
    interval: float = 1.0,
    once: bool = False,
    frames: int | None = None,
    fetch: Fetch | None = None,
    out: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Return the last frame. Unless `once`, redraw every `interval` seconds until `frames`
    frames are drawn or the user interrupts.
    """
    if not (math.isfinite(interval) and interval > 0):
        raise ValueError("--interval must be a positive number")
    config = lifecycle.instance(root)
    state = lifecycle._read_state(root) if (root / "lifecycle.json").exists() else {}
    if state.get("phase") not in PHASES:
        raise ValueError(
            f"dev watch requires a launched instance; this one is {state.get('phase', 'stopped')}"
        )
    url = config["router_url"]
    fetch = fetch or http_fetch
    if once:
        # Two readings one interval apart give the rates their window.
        first = parse(clock(), *fetch(url))
        sleep(interval)
        second = parse(clock(), *fetch(url))
        return view(None if restarted(first, second) else first, second)
    out = out or sys.stdout
    if out.isatty():
        return _live(url, _context(root, config), interval, frames, fetch, out, clock, sleep)
    # A file or pipe takes the frames one after another.
    window: deque[Sample] = deque()
    shown: dict[str, Any] = {"status": "observed", "engines": []}
    drawn = 0
    try:
        while frames is None or drawn < frames:
            try:
                sample = parse(clock(), *fetch(url))
                if window and restarted(window[-1], sample):
                    window.clear()
                window.append(sample)
                # The baseline is the oldest sample inside the window, or the one before this.
                while len(window) > 2 and sample.at - window[0].at > WINDOW_S:
                    window.popleft()
                shown = view(window[0] if len(window) > 1 else None, sample)
                text = render(shown, url)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
                text = f"narwhal dev watch  {url}\nrouter unreachable: {exc}\n"
            out.write("\n" + text)
            out.flush()
            drawn += 1
            if frames is None or drawn < frames:
                sleep(interval)
    except KeyboardInterrupt:
        pass
    return shown


def _live(
    url: str,
    context: Context,
    interval: float,
    frames: int | None,
    fetch: Fetch,
    out: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict:
    window: deque[Sample] = deque()
    history: deque[dict[str, Any]] = deque(maxlen=TREND_FRAMES - 1)
    shown: dict[str, Any] = {"status": "observed", "engines": []}
    roles: dict[str, str | None] = {}
    drawn = 0
    console = Console(file=out, force_terminal=True)
    # The alternate screen gives the terminal back as it was when the view closes.
    with Live(console=console, screen=True, auto_refresh=False) as live:
        try:
            while frames is None or drawn < frames:
                try:
                    sample = parse(clock(), *fetch(url))
                    if window and restarted(window[-1], sample):
                        window.clear()
                        history.clear()
                    window.append(sample)
                    while len(window) > 2 and sample.at - window[0].at > WINDOW_S:
                        window.popleft()
                    if shown.get("engines"):
                        history.append(shown)
                    shown = view(window[0] if len(window) > 1 else None, sample)
                    now = {engine["iid"]: engine["role"] for engine in shown["engines"]}
                    switched = {iid for iid, role in now.items() if roles.get(iid, role) != role}
                    roles = now
                    live.update(panel(shown, context, list(history), switched), refresh=True)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    live.update(
                        Panel(
                            Text(f"router unreachable: {exc}", style="red"),
                            title=f"narwhal dev watch  {url}",
                            border_style="red",
                        ),
                        refresh=True,
                    )
                drawn += 1
                if frames is None or drawn < frames:
                    sleep(interval)
        except KeyboardInterrupt:
            pass
    return shown
