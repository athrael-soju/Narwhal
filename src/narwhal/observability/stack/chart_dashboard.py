"""Write the dashboard the Helm chart's Grafana serves, rendered as `make observe` renders it.

`python -m narwhal.observability.stack.chart_dashboard <out.json>` writes the file that
`helm install --set-file monitoring.grafana.dashboard=<out.json>` hands the chart. With
`--console-url`, the dashboard also links to that fleet control console and shows its markers,
as `make observe` does.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from narwhal.observability.stack.artifacts import (
    BASE,
    CONSOLE_URL_ENV,
    console_url,
    narwhal_dashboard,
    render_dashboard,
)

SOURCE = BASE / "grafana-narwhal.json"


def chart_dashboard(console: str | None = None, source: Path = SOURCE) -> str:
    """The rendered dashboard, with the fleet control link and markers when `console` is set."""
    shipped = json.loads(source.read_text(encoding="utf-8"))
    dashboard = (
        render_dashboard(shipped) if console is None else narwhal_dashboard(shipped, console)
    )
    return json.dumps(dashboard, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path, help="file to write the rendered dashboard to")
    parser.add_argument("--console-url", help="fleet control console the dashboard links to")
    args = parser.parse_args()
    try:
        console = (
            None if args.console_url is None else console_url({CONSOLE_URL_ENV: args.console_url})
        )
        args.out.write_text(chart_dashboard(console), encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"wrote the dashboard to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
