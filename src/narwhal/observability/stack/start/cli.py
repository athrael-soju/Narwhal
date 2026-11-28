"""Start the shipped observability stack after proving listener and container identity."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from narwhal.observability.stack.artifacts import (
    ControlTarget,
    console_url,
    control_target,
    stage_artifacts,
    tool_target,
)
from narwhal.observability.stack.make_targets import (
    ALERTMANAGER_ENV,
    DEFAULT_FLEET,
    FLEET_ENV,
    ROUTER_URL_ENV,
    SIDECAR_URLS_ENV,
    TARGETS_URL_ENV,
    TargetContract,
    load_contract,
    router_urls,
)

from .listeners import check_listeners
from .readiness import DEFAULT_READY_TIMEOUT_S, HttpGet, http_get, wait_collection, wait_ready
from .services import Container, StartupError, configured_services
from .stack import ComposeStack, Stack, compose_problem


def start(
    env: Mapping[str, str],
    stack: Stack,
    contract: TargetContract,
    *,
    get: HttpGet = http_get,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    target_writer: Callable[
        [TargetContract, str, ControlTarget | None, str | None], None
    ] = stage_artifacts,
) -> dict[str, Container]:
    """Check listeners, launch Compose and verify the resulting services."""
    services = configured_services(env)
    console = console_url(env)
    control = control_target(env)
    tools = tool_target(env)
    check_listeners(services, stack)
    target_writer(contract, console, control, tools)
    stack.up()
    launched = wait_ready(services, stack, get=get, timeout_s=timeout_s)
    stack.protect_grafana()
    prometheus = next(service for service in services if service.name == "prometheus")
    for warning in wait_collection(prometheus, contract, get=get, timeout_s=timeout_s):
        print(f"warning: {warning}", file=sys.stderr)
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
        "--router-url",
        action="append",
        help=(
            "router HTTP origin scraped by Prometheus; repeat it for each router of a pair "
            f"(default: {ROUTER_URL_ENV}, with commas between the origins of a pair)"
        ),
    )
    parser.add_argument(
        "--targets-url",
        default=os.environ.get(TARGETS_URL_ENV),
        help=(
            "origin Prometheus reads the engine targets from, required for a router pair: "
            f"the load balancer that reaches the active router (default: {TARGETS_URL_ENV}, "
            "else the router)"
        ),
    )
    parser.add_argument(
        "--sidecar-url",
        action="append",
        help="attestation sidecar origin Prometheus scrapes for the restart counts and the host "
        f"down alert; repeat it for each engine (default: {SIDECAR_URLS_ENV}, with commas "
        "between origins)",
    )
    parser.add_argument(
        "--fleet-name",
        default=os.environ.get(FLEET_ENV) or DEFAULT_FLEET,
        help=f"the fleet label of every scraped series (default: {FLEET_ENV}, else "
        f"{DEFAULT_FLEET})",
    )
    parser.add_argument(
        "--alertmanager-url",
        default=os.environ.get(ALERTMANAGER_ENV) or None,
        help="Alertmanager Prometheus sends alerts to; without one, alerts show only on the "
        f"Prometheus alerts page (default: {ALERTMANAGER_ENV})",
    )
    args = parser.parse_args(argv)
    if args.ready_timeout <= 0:
        parser.error("--ready-timeout must be positive")
    routers = args.router_url or [os.environ.get(ROUTER_URL_ENV, "")]
    if not router_urls(routers):
        parser.error(f"--router-url or {ROUTER_URL_ENV} is required")
    try:
        contract = load_contract(
            routers,
            args.targets_url,
            sidecars=args.sidecar_url or [os.environ.get(SIDECAR_URLS_ENV, "")],
            fleet=args.fleet_name,
            alertmanager=args.alertmanager_url,
        )
        if problem := compose_problem():
            raise StartupError(problem)
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
