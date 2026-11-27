"""Write the stage and rerun tables in docs/Deploy.md, docs/deploy/Kubernetes.md and docs/cli/Dev.md
from the stage graphs.

Run it after changing `src/narwhal/deployment/deploy_graph.py`, `dev_graph.py` or
`kubernetes_graph.py`. With `--check`
it changes nothing and exits 1 when a page differs from its graphs; the unit suite runs that
check.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from narwhal import backends
from narwhal.deployment.deploy_graph import CAPABILITIES, DEPLOY_GRAPH
from narwhal.deployment.dev_graph import DIRECT_GRAPH, LAUNCHED_GRAPH
from narwhal.deployment.graph import StageGraph
from narwhal.deployment.kubernetes_graph import KUBERNETES_GRAPH
from narwhal.deployment.launch_engine import backend as launch_backend

ROOT = Path(__file__).resolve().parents[2]
END = "<!-- End of the generated stage tables. -->"


@dataclass(frozen=True)
class Block:
    """One generated block: the graph it renders and the page's path back to `docs/`."""

    graph: StageGraph
    source: str
    docs: str = ""

    @property
    def begin(self) -> str:
        return (
            f"<!-- Generated from {self.source} by tools/maintenance/stage_graph_doc.py. "
            "Edit the graph, then run the tool. -->"
        )


PAGES = {
    ROOT / "docs/Deploy.md": (Block(DEPLOY_GRAPH, "src/narwhal/deployment/deploy_graph.py"),),
    ROOT / "docs/deploy/Kubernetes.md": (
        Block(
            KUBERNETES_GRAPH,
            "KUBERNETES_GRAPH in src/narwhal/deployment/kubernetes_graph.py",
            "../",
        ),
    ),
    ROOT / "docs/cli/Dev.md": (
        Block(LAUNCHED_GRAPH, "LAUNCHED_GRAPH in src/narwhal/deployment/dev_graph.py", "../"),
        Block(DIRECT_GRAPH, "DIRECT_GRAPH in src/narwhal/deployment/dev_graph.py", "../"),
    ),
}


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


def render(page: Path, text: str) -> str:
    """Return `text` with each of the page's generated blocks replaced by its graph's tables."""
    for block in PAGES[page]:
        if text.count(block.begin) != 1:
            raise ValueError(f"{page} needs one block that begins {block.begin}")
        head, rest = text.split(block.begin)
        if END not in rest:
            raise ValueError(f"{page}: the block for {block.source} has no end marker")
        _, tail = rest.split(END, 1)
        tables = block.graph.markdown(capabilities(), docs=block.docs)
        text = f"{head}{block.begin}\n\n{tables}\n{END}{tail}"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when a page differs from its graphs"
    )
    args = parser.parse_args(argv)
    status = 0
    for page in PAGES:
        text = page.read_text()
        rendered = render(page, text)
        if not args.check:
            page.write_text(rendered)
        elif rendered != text:
            print(f"{page} differs from its stage graphs; run {__file__}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
