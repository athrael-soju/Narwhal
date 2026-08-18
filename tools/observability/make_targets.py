"""Write the Prometheus engine scrape targets from the fleet config.

    python3 tools/observability/make_targets.py config/fleet.local.json

Output goes to `runs/observability/targets/engines.json`, under `runs/`
because fleet deploys wipe the repo tree and leave `runs/` intact. The
output is untracked because it contains real engine addresses. Rerun after
any fleet change. Prometheus reloads the file without a restart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    cfg = json.loads(Path(sys.argv[1]).read_text())
    targets = [
        {
            "targets": [e["url"].removeprefix("http://").removeprefix("https://")],
            "labels": {"iid": e["iid"]},
        }
        for e in cfg["engines"]
    ]
    targets_dir = Path(__file__).parents[2] / "runs" / "observability" / "targets"
    out = targets_dir / "engines.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(targets, indent=2) + "\n")
    print(f"wrote {len(targets)} engine targets to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
