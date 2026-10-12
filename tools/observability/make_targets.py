"""Write the Prometheus router discovery target for one deployed Narwhal router."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_TARGETS_DIR = (
    Path(__file__).parents[2] / "runs" / "observability" / "mounts" / "prometheus" / "targets"
)
ENGINE_TARGETS_PATH = "/narwhal/targets"


@dataclass(frozen=True)
class TargetContract:
    """Expected Prometheus router target for one deployment."""

    router: str

    @property
    def engine_targets_url(self) -> str:
        return f"http://{self.router}{ENGINE_TARGETS_PATH}"


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


def load_contract(router_url: str) -> TargetContract:
    """Validate the router origin Prometheus scrapes."""
    return TargetContract(metrics_authority(router_url))


def write_contract(
    contract: TargetContract,
    targets_dir: Path = DEFAULT_TARGETS_DIR,
) -> None:
    """Write the router discovery document."""
    _write_json(targets_dir / "router.json", [{"targets": [contract.router]}])


def write_targets(router_url: str, targets_dir: Path = DEFAULT_TARGETS_DIR) -> TargetContract:
    """Generate the router discovery document and return its expected identity."""
    contract = load_contract(router_url)
    write_contract(contract, targets_dir)
    return contract


def main() -> int:
    """Generate the router discovery document for a deployed router."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router-url", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_TARGETS_DIR)
    args = parser.parse_args()
    try:
        contract = write_targets(args.router_url, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"wrote router {contract.router} to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
