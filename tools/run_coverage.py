"""Measure line and branch coverage across unit tests and CPU drill subprocesses."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import coverage

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "tools/coverage_support"


def commands(output):
    """Keep the measured cases aligned with make test."""
    return [
        ("unit", ["-m", "unittest", "discover", "-s", "tools/tests"]),
        ("ha", ["-m", "tools.coverage_support.ha", "--out", str(output / "ha")]),
        (
            "lifecycle-individual",
            ["tools/drills/lifecycle_restart.py", "--out", str(output / "individual")],
        ),
        (
            "lifecycle-wave",
            [
                "tools/drills/lifecycle_restart.py",
                "--restart-policy",
                "whole_wave",
                "--out",
                str(output / "wave"),
            ],
        ),
    ]


def verify_checkpoints(output):
    """Require saved data from every checkpoint, including the killed primary."""
    killed = list(output.glob("killed-*.json"))
    if not killed:
        raise RuntimeError("HA drill produced no acknowledged pre-kill checkpoint")
    for marker in [*output.glob("saved-*.json"), *killed]:
        data_path = Path(json.loads(marker.read_text())["data_file"])
        if not data_path.is_file():
            raise RuntimeError(f"checkpoint shard is missing: {marker.name}")
        data = coverage.CoverageData(basename=str(data_path))
        data.read()
        if not data.measured_files():
            raise RuntimeError(f"checkpoint shard is empty: {marker.name}")


def main(argv=None):
    """Run the CPU suite into a new output directory and write inspectable reports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="new artifact directory")
    args = parser.parse_args(argv)
    if args.out is None:
        parent = ROOT / "runs/coverage"
        parent.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    else:
        output = args.out.resolve()
        output.mkdir(parents=True, exist_ok=False)
    print(f"Coverage artifacts: {output}", flush=True)
    config = SUPPORT / "coverage.ini"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(SUPPORT), str(ROOT), str(ROOT / "src"))),
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "NARWHAL_COVERAGE_ACTIVE": "1",
        "COVERAGE_PROCESS_START": str(config),
        "COVERAGE_RCFILE": str(config),
        "NARWHAL_COVERAGE_ROOT": str(ROOT),
        "NARWHAL_COVERAGE_DIR": str(output),
    }
    results = []
    for context, command in commands(output):
        started = time.monotonic()
        with (output / f"{context}.log").open("w") as log:
            result = subprocess.run(
                [sys.executable, *command],
                cwd=ROOT,
                env={**env, "NARWHAL_COVERAGE_CONTEXT": context},
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        results.append(
            {
                "context": context,
                "command": command,
                "returncode": result.returncode,
                "elapsed_s": time.monotonic() - started,
            }
        )
        (output / "commands.json").write_text(json.dumps(results, indent=2) + "\n")
        print(f"{context}: exit {result.returncode}", flush=True)
        if result.returncode:
            print((output / f"{context}.log").read_text())
            return result.returncode
    verify_checkpoints(output)
    # Read source paths from the same config used by each subprocess.
    from unittest.mock import patch

    with patch.dict(os.environ, {**env, "NARWHAL_COVERAGE_CONTEXT": ""}):
        cov = coverage.Coverage(config_file=str(config))
        cov.combine(data_paths=[str(output)], keep=True)
        cov.save()
        cov.json_report(outfile=str(output / "coverage.json"), show_contexts=True)
        cov.xml_report(outfile=str(output / "coverage.xml"))
        cov.html_report(directory=str(output / "html"))
        with (output / "summary.txt").open("w") as summary:
            cov.report(file=summary)
        cov.report(include=["*/src/narwhal/*"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
