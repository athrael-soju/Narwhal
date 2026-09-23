"""Write Prometheus discovery targets from one deployed Narwhal fleet."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from narwhal.config.environment import resolve_endpoint

DEFAULT_TARGETS_DIR = (
    Path(__file__).parents[2] / "runs" / "observability" / "mounts" / "prometheus" / "targets"
)


@dataclass(frozen=True)
class TargetContract:
    """Expected Prometheus target identities for one deployment."""

    router: str
    engines: tuple[tuple[str, str], ...]


def metrics_authority(url: str) -> str:
    """Return the host and port Prometheus scrapes from an HTTP base URL."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid metrics URL {url!r}: {exc}") from exc
    if parsed.scheme != "http":
        raise ValueError(f"metrics URL {url!r} requires http")
    if parsed.hostname is None or port is None:
        raise ValueError(f"metrics URL {url!r} requires an explicit host and port")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"metrics URL {url!r} contains credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"metrics URL {url!r} must identify an HTTP origin")
    return parsed.netloc


def build_targets(fleet: dict[str, object], router_url: str) -> TargetContract:
    """Build router and engine target identities from deployment inputs."""
    raw_engines = fleet.get("engines")
    if not isinstance(raw_engines, list) or not raw_engines:
        raise ValueError("fleet document requires a populated engines array")
    engines: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, raw_engine in enumerate(raw_engines):
        if not isinstance(raw_engine, dict):
            raise ValueError(f"fleet engine {index} requires an object")
        iid = raw_engine.get("iid")
        url = raw_engine.get("url")
        if not isinstance(iid, str) or not iid:
            raise ValueError(f"fleet engine {index} requires iid")
        if iid in seen:
            raise ValueError(f"fleet engine iid {iid!r} appears more than once")
        if not isinstance(url, str):
            raise ValueError(f"fleet engine {iid!r} requires url")
        seen.add(iid)
        engines.append((iid, metrics_authority(resolve_endpoint(url, f"engines[{index}].url"))))
    return TargetContract(metrics_authority(router_url), tuple(engines))


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
