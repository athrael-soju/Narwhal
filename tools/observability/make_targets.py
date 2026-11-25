"""Write the Prometheus router discovery targets for one deployed Narwhal router or pair."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_TARGETS_DIR = (
    Path(__file__).parents[2] / "runs" / "observability" / "mounts" / "prometheus" / "targets"
)
ENGINE_TARGETS_PATH = "/narwhal/targets"
ROUTER_URL_ENV = "NARWHAL_ROUTER_URL"
TARGETS_URL_ENV = "NARWHAL_TARGETS_URL"


@dataclass(frozen=True)
class TargetContract:
    """Expected Prometheus router targets for one deployment."""

    # The router origins Prometheus scrapes, one per router.
    routers: tuple[str, ...]
    # The origin Prometheus reads the engine targets from.
    targets: str

    @property
    def engine_targets_url(self) -> str:
        return f"http://{self.targets}{ENGINE_TARGETS_PATH}"


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


def router_urls(values: Sequence[str]) -> list[str]:
    """Split repeated or comma-separated router URLs into one URL per router."""
    return [url.strip() for value in values for url in value.split(",") if url.strip()]


def load_contract(urls: Sequence[str], targets_url: str | None = None) -> TargetContract:
    """Validate the router origins Prometheus scrapes and the origin of the engine targets.

    One router serves its own engine targets. A router pair needs `targets_url`, an origin that
    reaches the active router, such as the load balancer in front of the pair.
    """
    routers = tuple(metrics_authority(url) for url in router_urls(urls))
    if not routers:
        raise ValueError(f"--router-url or {ROUTER_URL_ENV} requires at least one router origin")
    if len(set(routers)) != len(routers):
        raise ValueError(f"router origins {', '.join(routers)} repeat a router")
    if targets_url:
        targets = metrics_authority(targets_url)
    elif len(routers) == 1:
        targets = routers[0]
    else:
        raise ValueError(
            f"a router pair requires --targets-url or {TARGETS_URL_ENV}: "
            "the load balancer origin that reaches the active router"
        )
    return TargetContract(routers, targets)


def write_contract(
    contract: TargetContract,
    targets_dir: Path = DEFAULT_TARGETS_DIR,
) -> None:
    """Write the router discovery document."""
    _write_json(targets_dir / "router.json", [{"targets": list(contract.routers)}])


def write_targets(
    urls: Sequence[str],
    targets_url: str | None = None,
    targets_dir: Path = DEFAULT_TARGETS_DIR,
) -> TargetContract:
    """Generate the router discovery document and return its expected identity."""
    contract = load_contract(urls, targets_url)
    write_contract(contract, targets_dir)
    return contract


def main() -> int:
    """Generate the router discovery document for a deployed router or router pair."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--router-url",
        action="append",
        required=True,
        help="router HTTP origin; repeat it, or separate origins with commas, for a router pair",
    )
    parser.add_argument("--targets-url", help="origin that reaches the active router of a pair")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_TARGETS_DIR)
    args = parser.parse_args()
    try:
        contract = write_targets(args.router_url, args.targets_url, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"wrote routers {', '.join(contract.routers)} to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
