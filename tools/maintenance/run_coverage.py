"""Measure unit-test coverage and write reports to a run directory."""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import coverage

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    """Run unit tests under coverage and retain reports."""
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

    data_file = output / ".coverage"
    command = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        "--branch",
        "--source=src/narwhal,tools",
        "--data-file",
        str(data_file),
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-t",
        ".",
    ]
    started = time.monotonic()
    with (output / "unit.log").open("w") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    (output / "command.json").write_text(
        json.dumps(
            {
                "command": command,
                "returncode": result.returncode,
                "elapsed_s": time.monotonic() - started,
            },
            indent=2,
        )
        + "\n"
    )
    if result.returncode:
        print((output / "unit.log").read_text())
        return result.returncode

    measured = coverage.Coverage(data_file=str(data_file))
    measured.load()
    measured.json_report(outfile=str(output / "coverage.json"))
    measured.xml_report(outfile=str(output / "coverage.xml"))
    measured.html_report(directory=str(output / "html"))
    with (output / "summary.txt").open("w") as summary:
        measured.report(file=summary)
    measured.report(include=["*/src/narwhal/*"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
