"""vLLM engine launch."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ...deployment.launch_engine import captures
from ...deployment.launch_engine import check as runtime_check
from ...deployment.launch_engine import plan as launch_plan
from ...deployment.launch_engine.backend import EngineLauncher


class VllmLauncher(EngineLauncher):
    """Launch vLLM with the NIXL connector in both KV roles."""

    def build(
        self, record: dict, env: Mapping[str, str], output: Path, *, backend: str
    ) -> tuple[dict, dict[str, str]]:
        """Build the vLLM launch plan."""
        return launch_plan.build(record, dict(env), output, backend=backend)

    def check(self, run: Path, plan: dict) -> None:
        """Run the in-image vLLM and NIXL check."""
        runtime_check.check(run, plan)

    def capture_cache(self, run: Path, plan: dict) -> None:
        """Capture vLLM's KV cache layout."""
        captures.capture_cache(run, plan)
