"""Start the shipped observability stack after proving listener and container identity."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from tools.observability.artifacts import console_url, stage_artifacts
from tools.observability.make_targets import TargetContract, load_contract

from .listeners import check_listeners
from .readiness import DEFAULT_READY_TIMEOUT_S, HttpGet, http_get, wait_collection, wait_ready
from .services import Container, StartupError, configured_services
from .stack import ComposeStack, Stack


def start(
    env: Mapping[str, str],
    stack: Stack,
    contract: TargetContract,
    *,
    get: HttpGet = http_get,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    target_writer: Callable[[TargetContract, str], None] = stage_artifacts,
) -> dict[str, Container]:
    """Check listeners, launch Compose and verify the resulting services."""
    services = configured_services(env)
    console = console_url(env)
    check_listeners(services, stack)
    target_writer(contract, console)
    stack.up()
    launched = wait_ready(services, stack, get=get, timeout_s=timeout_s)
    prometheus = next(service for service in services if service.name == "prometheus")
    wait_collection(prometheus, contract, get=get, timeout_s=timeout_s)
    for service in services:
        container = launched[service.name]
        print(
            f"{service.label} ready at {service.listener.url} "
            f"({container.cid[:12]}, {container.image})"
        )
    return launched


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checked observability startup command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=DEFAULT_READY_TIMEOUT_S,
        help="seconds to wait for both versioned health contracts",
    )
    parser.add_argument(
        "--fleet",
        type=Path,
        default=os.environ.get("NARWHAL_FLEET"),
        help="deployed fleet document (default: NARWHAL_FLEET)",
    )
    parser.add_argument(
        "--router-url",
        default=os.environ.get("NARWHAL_ROUTER_URL"),
        help="router HTTP origin scraped by Prometheus (default: NARWHAL_ROUTER_URL)",
    )
    args = parser.parse_args(argv)
    if args.ready_timeout <= 0:
        parser.error("--ready-timeout must be positive")
    if args.fleet is None:
        parser.error("--fleet or NARWHAL_FLEET is required")
    if args.router_url is None:
        parser.error("--router-url or NARWHAL_ROUTER_URL is required")
    try:
        contract = load_contract(args.fleet, args.router_url)
        start(os.environ, ComposeStack(env=os.environ), contract, timeout_s=args.ready_timeout)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"observability startup failed: {detail}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        command = " ".join(str(part) for part in exc.cmd)
        print(
            f"observability startup failed: command exceeded {exc.timeout:g}s: {command}",
            file=sys.stderr,
        )
        return 1
    except (StartupError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"observability startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
