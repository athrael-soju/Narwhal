"""Convert public LLM inference traces to `mooncake_trace` files for timestamped replay."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

FORMATS = ("azure", "burstgpt", "mooncake")
# Source column names of each CSV format: arrival time, input tokens, output tokens.
COLUMNS = {
    "azure": ("TIMESTAMP", "ContextTokens", "GeneratedTokens"),
    "burstgpt": ("Timestamp", "Request tokens", "Response tokens"),
}


@dataclass(frozen=True)
class Arrival:
    """One source request: its arrival in source seconds and its token counts."""

    at_s: float
    input_length: int
    output_length: int
    hash_ids: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Conversion:
    """What a conversion wrote and skipped."""

    requests: int
    skipped: int
    source_span_s: float
    replay_span_s: float

    def offered_rps(self) -> float | None:
        """Return the mean arrival rate of the replay."""
        return mean_rate(self.requests, self.replay_span_s)


def mean_rate(requests: int, span_s: float) -> float | None:
    """Return arrival intervals per second over `span_s`, or None without an interval."""
    if requests < 2 or span_s <= 0:
        return None
    return (requests - 1) / span_s


def _count(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"{where}: token count must be an integer")
    try:
        count = int(value)
    except ValueError as exc:
        raise ValueError(f"{where}: token count must be an integer") from exc
    if count < 0:
        raise ValueError(f"{where}: token count must not be negative")
    return count


def _csv_rows(path: Path, kind: str) -> Iterator[Arrival]:
    time_key, input_key, output_key = COLUMNS[kind]
    origin: datetime | None = None
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        missing = [key for key in COLUMNS[kind] if key not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(f"{path}: missing {kind} columns: {', '.join(missing)}")
        for line, row in enumerate(reader, 2):
            where = f"{path}:{line}"
            raw = row[time_key]
            try:
                if kind == "azure":
                    stamp = datetime.fromisoformat(raw)
                    origin = origin or stamp
                    at_s = (stamp - origin).total_seconds()
                else:
                    at_s = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{where}: unreadable timestamp {raw!r}") from exc
            if not math.isfinite(at_s):
                raise ValueError(f"{where}: unreadable timestamp {raw!r}")
            yield Arrival(at_s, _count(row[input_key], where), _count(row[output_key], where))


def _mooncake_rows(path: Path) -> Iterator[Arrival]:
    with path.open(encoding="utf-8") as stream:
        for line, text in enumerate(stream, 1):
            if not text.strip():
                continue
            where = f"{path}:{line}"
            try:
                row = json.loads(text)
                stamp = row["timestamp"]
                if isinstance(stamp, bool) or not isinstance(stamp, int | float):
                    raise TypeError
                hash_ids = row.get("hash_ids")
                if hash_ids is not None and (
                    not isinstance(hash_ids, list)
                    or any(isinstance(h, bool) or not isinstance(h, int) for h in hash_ids)
                ):
                    raise TypeError
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f"{where}: not a mooncake_trace record") from exc
            if not math.isfinite(stamp):
                raise ValueError(f"{where}: unreadable timestamp {stamp!r}")
            yield Arrival(
                stamp / 1000,
                _count(row.get("input_length"), where),
                _count(row.get("output_length"), where),
                None if hash_ids is None else tuple(hash_ids),
            )


def read_arrivals(path: Path, kind: str) -> Iterator[Arrival]:
    """Yield the requests of a source trace in file order."""
    if kind == "mooncake":
        return _mooncake_rows(path)
    if kind in COLUMNS:
        return _csv_rows(path, kind)
    raise ValueError(f"format must be one of {', '.join(FORMATS)}")


def convert(
    arrivals: Iterator[Arrival],
    time_scale: float = 1.0,
    start_s: float = 0.0,
    duration_s: float | None = None,
    max_total_tokens: int | None = None,
) -> tuple[list[dict], Conversion]:
    """Return the clipped, rebased and time-scaled `mooncake_trace` records.

    The clip keeps requests that arrive from `start_s` to before `start_s + duration_s`
    seconds after the first source request. The first kept request replays at 0, and
    each gap is divided by `time_scale`, so the offered rate rises by that factor.
    Requests whose input and output tokens together exceed `max_total_tokens` are skipped.
    """
    if not (math.isfinite(time_scale) and time_scale > 0):
        raise ValueError("time scale must be finite and positive")
    if not (math.isfinite(start_s) and start_s >= 0):
        raise ValueError("start must be a non-negative number of seconds")
    if duration_s is not None and not (math.isfinite(duration_s) and duration_s > 0):
        raise ValueError("duration must be a positive number of seconds")
    if max_total_tokens is not None and max_total_tokens < 2:
        raise ValueError("max total tokens must be at least 2")
    kept: list[Arrival] = []
    skipped = 0
    origin: float | None = None
    for arrival in arrivals:
        origin = arrival.at_s if origin is None else origin
        offset = arrival.at_s - origin
        if offset < start_s or (duration_s is not None and offset >= start_s + duration_s):
            continue
        # AIPerf needs at least one input token, and a request that produced no
        # output tokens failed at the source.
        if (
            arrival.input_length < 1
            or arrival.output_length < 1
            or (
                max_total_tokens is not None
                and arrival.input_length + arrival.output_length > max_total_tokens
            )
        ):
            skipped += 1
            continue
        kept.append(arrival)
    kept.sort(key=lambda arrival: arrival.at_s)
    if not kept:
        raise ValueError("the clip holds no request with input and output tokens")
    first = kept[0].at_s
    records = []
    for arrival in kept:
        record: dict = {
            "timestamp": round((arrival.at_s - first) * 1000 / time_scale, 3),
            "input_length": arrival.input_length,
            "output_length": arrival.output_length,
        }
        if arrival.hash_ids is not None:
            record["hash_ids"] = list(arrival.hash_ids)
        records.append(record)
    source_span = kept[-1].at_s - first
    return records, Conversion(len(records), skipped, source_span, source_span / time_scale)


def write_trace(path: Path, records: list[dict]) -> None:
    """Write `records` as a new owner-only JSONL file."""
    with open(
        path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)
    ) as stream:
        for record in records:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", required=True, choices=FORMATS, help="source trace format")
    parser.add_argument("--input", required=True, type=Path, help="source trace file")
    parser.add_argument("--out", required=True, type=Path, help="new mooncake_trace JSONL file")
    parser.add_argument(
        "--time-scale",
        type=float,
        default=1.0,
        help="factor that divides every arrival gap and multiplies the offered rate",
    )
    parser.add_argument("--start-s", type=float, default=0.0, help="clip start, in source seconds")
    parser.add_argument("--duration-s", type=float, help="clip length, in source seconds")
    parser.add_argument(
        "--max-total-tokens",
        type=int,
        help="skip requests whose input and output tokens together exceed this count",
    )
    args = parser.parse_args(argv)
    try:
        records, result = convert(
            read_arrivals(args.input, args.format),
            args.time_scale,
            args.start_s,
            args.duration_s,
            args.max_total_tokens,
        )
        write_trace(args.out, records)
    except (OSError, ValueError) as error:
        print(f"Conversion failed: {error}", file=sys.stderr)
        return 1
    rate = result.offered_rps()
    print(
        json.dumps(
            {
                "requests": result.requests,
                "skipped": result.skipped,
                "source_span_s": round(result.source_span_s, 3),
                "replay_span_s": round(result.replay_span_s, 3),
                "offered_rps": None if rate is None else round(rate, 4),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
