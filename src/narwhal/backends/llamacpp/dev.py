from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ...deployment.direct_start import DirectEngine, DirectStart, NominalFigures

VERSION_TIMEOUT_S = 20
_BUILD = re.compile(r"\(build (\d+), commit ([0-9a-f]+)\)")
# Long enough for a stable prefill rate, short enough to keep startup in seconds.
PROBE_PROMPT = " ".join(["The quick brown fox jumps over the lazy dog."] * 12)
PROBE_OUTPUT_TOKENS = 16
# Layers past a model's depth are ignored, so this places every layer on the accelerator.
ALL_LAYERS = "999"


def _positive(timings: Any, name: str) -> float:
    value = timings.get(name) if isinstance(timings, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise ValueError(f"the engine reply carries no positive timings.{name}")
    return float(value) / 1000


class LlamacppDirect(DirectStart):
    def environment(self, binary: Path) -> dict[str, str]:
        # A prebuilt release keeps its shared libraries beside the server binary.
        inherited = os.environ.get("LD_LIBRARY_PATH")
        return {"LD_LIBRARY_PATH": os.pathsep.join(filter(None, [str(binary.parent), inherited]))}

    def check(self, binary: Path, runtime: dict[str, Any]) -> str:
        try:
            result = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_S,
                env={**os.environ, **self.environment(binary)},
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"{binary} --version failed: {exc}") from exc
        # The server prints its version on stderr.
        found = _BUILD.search(result.stdout + result.stderr)
        if result.returncode or found is None:
            raise ValueError(f"{binary} --version reported no llama.cpp build")
        build = int(found.group(1))
        if build != runtime["build"]:
            raise ValueError(f"template requires llama.cpp build {runtime['build']}; found {build}")
        return f"b{build}-{found.group(2)}"

    def command(self, binary: Path, engine: DirectEngine) -> list[str]:
        runtime = engine.runtime
        slots = runtime["slots"]
        return [
            str(binary),
            "--model",
            str(engine.model),
            "--alias",
            engine.served_name,
            "--host",
            engine.host,
            "--port",
            str(engine.port),
            # The server divides its context among its slots.
            "--ctx-size",
            str(runtime["context_tokens"] * slots),
            "--parallel",
            str(slots),
            "--threads",
            str(engine.threads),
            "--n-gpu-layers",
            ALL_LAYERS if engine.accelerated else "0",
            "--slot-save-path",
            str(engine.shared_dir),
            "--metrics",
            "--slots",
            "--no-webui",
            *runtime.get("extra_args", []),
        ]

    def probe(self, served_name: str) -> tuple[str, dict[str, Any]]:
        body = {
            "model": served_name,
            "prompt": PROBE_PROMPT,
            "max_tokens": PROBE_OUTPUT_TOKENS,
            "temperature": 0,
            "cache_prompt": False,
        }
        return "/v1/completions", body

    def figures(self, reply: dict[str, Any], runtime: dict[str, Any]) -> NominalFigures:
        timings = reply.get("timings")
        return NominalFigures(
            prefill_token_s=_positive(timings, "prompt_per_token_ms"),
            token_interval_s=_positive(timings, "predicted_per_token_ms"),
            kv_capacity_tokens=runtime["context_tokens"] * runtime["slots"],
            max_requests=runtime["slots"],
        )
