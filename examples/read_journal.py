"""Score a router journal offline: the run's record is the file, not the run.

Every journal opens with a provenance row naming the build that wrote it, and
every request row carries the run id. `narwhal-bench --score-journal` does the
same scoring from the command line; this is the Python shape of it.

    .venv/bin/python examples/read_journal.py runs/local/journal.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from narwhal.bench import score_journal


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])

    first = json.loads(path.read_text().splitlines()[0])
    if "meta" in first:
        meta = first["meta"]
        print(f"written by {meta['package']} {meta['version']} ({meta.get('git') or 'no git'})")

    # The SLOs are yours, not the file's: the journal records what happened,
    # and attainment is a question you ask it.
    frac, met, total = score_journal(path, ttft_slo=10.0, tpot_slo=0.125)
    print(f"{frac * 100:.1f}% attainment, {met}/{total} requests met both targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
