"""Write Prometheus targets for the engines, from the fleet config.

    python3 tools/observability/make_targets.py config/fleet.local.json

The output lands in `runs/observability/targets/engines.json` - under
`runs/` because that is the one tree a fleet deploy preserves: the old
location beside this script was wiped by every deploy, which orphaned the
container's bind mount and turned every node card OFFLINE until someone
noticed. It is not tracked because it carries real addresses. Rerun after
any fleet change; Prometheus picks the file up without a restart.
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
