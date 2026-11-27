"""Write the stage and rerun tables in docs/Deploy.md from the deployment stage graph.

Run it after changing `src/narwhal/deployment/deploy_graph.py`. With `--check` it changes
nothing and exits 1 when the page differs from the graph; the unit suite runs that check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from narwhal import backends
from narwhal.deployment.deploy_graph import CAPABILITIES, DEPLOY_GRAPH
from narwhal.deployment.launch_engine import backend as launch_backend

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs/Deploy.md"
BEGIN = (
    "<!-- Generated from src/narwhal/deployment/deploy_graph.py by "
    "tools/maintenance/stage_graph_doc.py. Edit the graph, then run the tool. -->"
)
END = "<!-- End of the generated stage tables. -->"


def capabilities() -> dict[str, str]:
    """Where each capability-limited stage runs: the backends whose launcher has it.

    A capability is named as the `launch_engine.backend` function that finds it on a launcher.
    While this build has no such function, or no backend has the capability, the page gives
    the capability's meaning from the graph instead of backend names.
    """
    labels = {}
    for capability, meaning in CAPABILITIES.items():
        finder = getattr(launch_backend, capability, None)
        names = (
            []
            if finder is None
            else [
                backend.label
                for backend in map(backends.load, backends.names())
                if backend.launcher is not None and finder(backend.engine_launcher()) is not None
            ]
        )
        labels[capability] = " or ".join(names) if names else meaning
    return labels


def render(text: str) -> str:
    """Return `text` with the generated block replaced by the current graph's tables."""
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) > text.index(END):
        raise ValueError(f"{PAGE} needs one generated block between its markers")
    head, rest = text.split(BEGIN)
    _, tail = rest.split(END)
    return f"{head}{BEGIN}\n\n{DEPLOY_GRAPH.markdown(capabilities())}\n{END}{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when the page differs from the graph"
    )
    args = parser.parse_args(argv)
    text = PAGE.read_text()
    rendered = render(text)
    if args.check:
        if rendered != text:
            print(f"{PAGE} differs from the deployment stage graph; run {__file__}")
            return 1
        return 0
    PAGE.write_text(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
