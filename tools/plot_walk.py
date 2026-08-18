#!/usr/bin/env python3
"""Draw a walk arm's topology over time as a standalone SVG.

Input is the router log: each monitoring pass logs `loop | Lp=.. Ld=.. | nPnD`
with a wall-clock stamp, which is the pool split at one-second resolution.
Phase boundaries come from the segment spec; the first loop line is t=0.

    plot_walk.py --log planner.seed7.router.log --segments "<spec>" \
        --out walk.svg [--labels "4P2D,5P1D,..."]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from narwhal.trace import parse_segments

LINE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*loop \| Lp=([\d.]+) Ld=([\d.]+) \| (\d)P(\d)D"
)

W, H, PAD, BAND_H = 1200, 360, 56, 40


def parse_log(path: Path) -> list[tuple[float, float, float, int, int]]:
    out = []
    t0 = None
    for line in path.read_text().splitlines():
        m = LINE.search(line)
        if not m:
            continue
        at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        if t0 is None:
            t0 = at
        out.append(
            (
                (at - t0).total_seconds(),
                float(m.group(2)),
                float(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
            )
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--segments", required=True)
    ap.add_argument("--labels", default="")
    ap.add_argument("--out", default="walk.svg")
    args = ap.parse_args()

    points = parse_log(Path(args.log))
    if not points:
        print("no loop lines found", file=sys.stderr)
        return 1
    segments = parse_segments(args.segments)
    labels = [x.strip() for x in args.labels.split(",")] if args.labels else []
    total = max(points[-1][0], sum(d for d, *_ in segments))
    x = lambda t: PAD + (W - 2 * PAD) * t / total
    y = lambda p: H - PAD - (H - 2 * PAD - BAND_H) * (p - 1) / 4

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="system-ui, sans-serif" font-size="13">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        f'<text x="{PAD}" y="24" font-size="16" font-weight="600">'
        f"Prefill pool size over the walk (of 6 engines)</text>",
    ]
    # Phase bands and labels.
    t = 0.0
    for k, (dur, _, _, _) in enumerate(segments):
        x0, x1 = x(t), x(min(t + dur, total))
        fill = "#f4f6f8" if k % 2 else "#e9eef3"
        svg.append(
            f'<rect x="{x0:.1f}" y="{PAD}" width="{x1 - x0:.1f}" '
            f'height="{H - 2 * PAD}" fill="{fill}"/>'
        )
        name = labels[k] if k < len(labels) else f"P{k + 1}"
        svg.append(
            f'<text x="{(x0 + x1) / 2:.1f}" y="{PAD + 16}" '
            f'text-anchor="middle" fill="#556">{name}</text>'
        )
        t += dur
    # Gridlines per split.
    for p in range(1, 6):
        yy = y(p)
        svg.append(
            f'<line x1="{PAD}" y1="{yy:.1f}" x2="{W - PAD}" y2="{yy:.1f}" '
            f'stroke="#d5dbe1" stroke-dasharray="3 4"/>'
        )
        svg.append(
            f'<text x="{PAD - 8}" y="{yy + 4:.1f}" text-anchor="end" '
            f'fill="#556">{p}P{6 - p}D</text>'
        )
    # The staircase.
    d = []
    for i, (tt, _, _, npf, _) in enumerate(points):
        cmd = "M" if not d else "L"
        if d and npf != points[i - 1][3]:
            d.append(f"L{x(tt):.1f},{y(points[i - 1][3]):.1f}")
        d.append(f"{cmd}{x(tt):.1f},{y(npf):.1f}")
    svg.append(f'<path d="{" ".join(d)}" fill="none" stroke="#1f6feb" stroke-width="2.5"/>')
    svg.append("</svg>")
    Path(args.out).write_text("\n".join(svg) + "\n")
    flips = sum(1 for i in range(1, len(points)) if points[i][3] != points[i - 1][3])
    print(f"{args.out}: {len(points)} passes, {flips} pool changes, {total:.0f}s span")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
