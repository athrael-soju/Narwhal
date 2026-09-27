"""Write Prometheus discovery targets from one deployed Narwhal fleet."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from narwhal.observability.management_targets import (
    TargetContract,
    build_targets,
    metrics_authority,  # noqa: F401 - preserve the operator helper import
)

DEFAULT_TARGETS_DIR = (
    Path(__file__).parents[2] / "runs" / "observability" / "mounts" / "prometheus" / "targets"
)


def _write_json(path: Path, value: object) -> None:
    """Replace one discovery document after its complete contents reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_contract(fleet_path: Path, router_url: str) -> TargetContract:
    """Load and validate the expected target identities for one deployment."""
    fleet = json.loads(fleet_path.read_text())
    return build_targets(fleet, router_url)


def write_contract(
    contract: TargetContract,
    targets_dir: Path = DEFAULT_TARGETS_DIR,
) -> None:
    """Write both discovery documents from a validated deployment contract."""
    _write_json(targets_dir / "router.json", [{"targets": [contract.router]}])
    _write_json(
        targets_dir / "engines.json",
        [{"targets": [authority], "labels": {"iid": iid}} for iid, authority in contract.engines],
    )


def write_targets(
    fleet_path: Path,
    router_url: str,
    targets_dir: Path = DEFAULT_TARGETS_DIR,
) -> TargetContract:
    """Generate both discovery documents and return their expected identities."""
    contract = load_contract(fleet_path, router_url)
    write_contract(contract, targets_dir)
    return contract


def main() -> int:
    """Generate discovery documents for a deployed router and fleet."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fleet", type=Path)
    parser.add_argument("--router-url", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_TARGETS_DIR)
    args = parser.parse_args()
    try:
        contract = write_targets(args.fleet, args.router_url, args.output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(
        f"wrote router {contract.router} and {len(contract.engines)} engine targets "
        f"to {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
