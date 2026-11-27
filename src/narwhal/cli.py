"""Process entry points."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import socket
import sys
import time
from pathlib import Path

import uvicorn

from .cli_errors import failure
from .cli_support import add_version_argument
from .config import FleetConfig
from .runtime.lease import DEFAULT_SAFETY_MARGIN_S, FileLease, LeaseError
from .runtime.listeners import check_http_bind
from .runtime.standby import MAX_HANDOFF_AGE_S
from .serving.app import create_app

# uvicorn logs its shutdown steps here.
server_log = logging.getLogger("uvicorn.error")

# Map uvicorn's trace level to logging's DEBUG.
# Seconds a stopping router keeps serving after its readiness fails and before it stops
# listening. The shipped HAProxy configuration checks every 0.2 s, so it takes the router out
# of rotation before the router closes a connection HAProxy might open or reuse.
SHUTDOWN_DRAIN_S = 0.5

LOG_LEVELS = {
    "critical": "CRITICAL",
    "error": "ERROR",
    "warning": "WARNING",
    "info": "INFO",
    "debug": "DEBUG",
    "trace": "DEBUG",
}


def _first_start(args: argparse.Namespace) -> bool:
    """Whether a `--first-start-primary` router starts as the primary and claims the lease.

    It does when no lease exists, or when the lease expired longer ago than the
    `--standby-max-handoff-age`, since no standby then holds a handoff fresh enough to take
    over. A live lease, or one a standby can still take over, makes it the standby of its
    peer, as does a lease directory without its `--lease-witness`.
    """
    lease = FileLease(
        Path(args.lease_path),
        "first-start",
        args.lease_ttl,
        witness=Path(args.lease_witness) if args.lease_witness else None,
    )
    try:
        record = lease.read()
    except (LeaseError, ValueError):
        # A missing directory or witness, or a lease this version cannot read.
        return False
    return record is None or time.time() - record.expires_at > args.standby_max_handoff_age


def serve(argv: list[str] | None = None) -> int:
    """Run the serving CLI."""
    ap = argparse.ArgumentParser(description="Run Narwhal over a disaggregated engine fleet")
    add_version_argument(ap)
    ap.add_argument("--fleet", required=True, help="fleet config JSON")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    ap.add_argument("--port", type=int, default=8000, help="TCP port (default: %(default)s)")
    ap.add_argument(
        "--log-level",
        default="info",
        choices=sorted(LOG_LEVELS),
        help="logging threshold (default: %(default)s)",
    )
    ap.add_argument(
        "--journal",
        default="",
        help="append the per-request journal here, one file per run "
        "(default journal.jsonl beside the profiles)",
    )
    ap.add_argument(
        "--max-concurrent",
        type=int,
        default=None,
        help="requests served at once, the rest answered 429 "
        "(default: the config's max_connections, the engine pool)",
    )
    ap.add_argument(
        "--graceful-timeout",
        type=int,
        default=None,
        help="nonnegative integer seconds for in-flight requests to finish on shutdown "
        "(default: the config's graceful_timeout_s)",
    )
    ap.add_argument(
        "--shutdown-drain",
        type=float,
        default=SHUTDOWN_DRAIN_S,
        help="finite nonnegative seconds a stopping router keeps serving after its readiness "
        "fails and before it stops listening, longer than the load balancer's health-check "
        f"interval (default {SHUTDOWN_DRAIN_S:g})",
    )
    ap.add_argument(
        "--standby-of",
        default="",
        metavar="URL",
        help="run as a warm standby of the primary router at URL: remain "
        "non-ready while shadowing its handoff, then attempt fenced takeover "
        "after the primary is silent and its shared lease expires",
    )
    ap.add_argument(
        "--first-start-primary",
        action="store_true",
        help="start as the primary while no lease file exists at --lease-path, and as the "
        "standby of --standby-of once one does, so a supervisor's restart rejoins the pair",
    )
    ap.add_argument(
        "--standby-probe-interval",
        type=float,
        default=None,
        help="finite positive seconds between standby probes of the primary (default 0.25)",
    )
    ap.add_argument(
        "--standby-takeover-after",
        type=int,
        default=None,
        help="consecutive failed probes before the standby takes over, at least 1 (default 4)",
    )
    ap.add_argument(
        "--standby-max-handoff-age",
        type=float,
        default=MAX_HANDOFF_AGE_S,
        help="finite positive maximum state age in seconds for automatic takeover (default 30)",
    )
    ap.add_argument(
        "--lease-path",
        default="",
        help="lease JSON on storage shared by the primary and standby",
    )
    ap.add_argument(
        "--lease-witness",
        default="",
        help="file on the shared lease storage that must exist before the router touches the "
        "lease, such as the one narwhal deploy's lease check leaves beside it",
    )
    ap.add_argument(
        "--router-id",
        default="",
        help="stable router name reported with its unique lease holder token",
    )
    ap.add_argument(
        "--lease-ttl",
        type=float,
        default=5.0,
        help="finite lease lifetime in seconds, must exceed --lease-renew-interval plus "
        "--lease-safety-margin (default: %(default)s)",
    )
    ap.add_argument(
        "--lease-renew-interval",
        type=float,
        default=1.0,
        help="finite positive seconds between lease renewals (default: %(default)s)",
    )
    ap.add_argument(
        "--lease-safety-margin",
        type=float,
        default=DEFAULT_SAFETY_MARGIN_S,
        help="finite nonnegative relative clock skew in seconds reserved before expiry "
        "(default: %(default)s)",
    )
    ap.add_argument(
        "--deployment-status",
        default="",
        metavar="PATH",
        help="narwhal.deployment-status document whose stage states, times, fabric edges and "
        "attested engines /metrics exports, read on every scrape",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="restore roles, breaker holds, and counters from the last state handoff",
    )
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if not 0 <= args.port <= 65535:
        ap.error(f"--port must be between 0 and 65535, got {args.port}")
    if args.max_concurrent is not None and args.max_concurrent < 1:
        ap.error(f"--max-concurrent must be at least 1, got {args.max_concurrent}")
    # uvicorn counts this forward from SIGTERM.
    if args.graceful_timeout is not None and args.graceful_timeout < 0:
        ap.error(f"--graceful-timeout cannot be negative, got {args.graceful_timeout}")
    if not (math.isfinite(args.shutdown_drain) and args.shutdown_drain >= 0):
        ap.error(f"--shutdown-drain must be finite and nonnegative, got {args.shutdown_drain}")

    logging.basicConfig(
        level=LOG_LEVELS[args.log_level],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs one INFO line per engine leg.
    if LOG_LEVELS[args.log_level] != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if (error := _port_in_use(args.port, args.host)) is not None:
        return failure("narwhal-serve", f"bind {args.host}:{args.port}", OSError(error), 1)

    try:
        cfg = FleetConfig.load(args.fleet)
    except (OSError, ValueError) as exc:
        return failure("narwhal-serve", f"load fleet {args.fleet}", exc, 2)
    if args.max_concurrent is not None and args.max_concurrent > cfg.max_connections:
        ap.error(
            f"--max-concurrent {args.max_concurrent} exceeds the fleet's "
            f"max_connections {cfg.max_connections}: admission would outrun the dispatch pool"
        )
    if args.resume:
        cfg.resume = True
    for name in (
        "lease_ttl",
        "lease_renew_interval",
        "lease_safety_margin",
        "standby_probe_interval",
        "standby_max_handoff_age",
    ):
        value = getattr(args, name)
        if value is not None and not math.isfinite(value):
            ap.error(f"--{name.replace('_', '-')} must be finite")
    if args.standby_probe_interval is not None and args.standby_probe_interval <= 0:
        ap.error("--standby-probe-interval must be positive")
    if args.standby_takeover_after is not None and args.standby_takeover_after < 1:
        ap.error("--standby-takeover-after must be at least 1")
    if args.standby_max_handoff_age <= 0:
        ap.error("--standby-max-handoff-age must be positive")
    if args.standby_of and not args.lease_path:
        ap.error("--standby-of requires --lease-path for fenced takeover")
    if args.first_start_primary and not args.standby_of:
        ap.error("--first-start-primary requires --standby-of")
    if args.lease_witness and not args.lease_path:
        ap.error("--lease-witness requires --lease-path")
    if args.first_start_primary and _first_start(args):
        args.standby_of = ""
    if args.lease_safety_margin < 0:
        ap.error("--lease-safety-margin must be nonnegative")
    if (
        args.lease_renew_interval <= 0
        or args.lease_ttl <= args.lease_renew_interval + args.lease_safety_margin
    ):
        ap.error("--lease-ttl must exceed --lease-renew-interval plus --lease-safety-margin")
    try:
        app = create_app(
            cfg,
            max_concurrent=args.max_concurrent,
            journal_path=args.journal or None,
            standby_of=args.standby_of.rstrip("/") or None,
            standby_probe_interval_s=args.standby_probe_interval,
            standby_takeover_after=args.standby_takeover_after,
            standby_max_handoff_age_s=args.standby_max_handoff_age,
            lease_path=args.lease_path or None,
            lease_witness=args.lease_witness or None,
            router_id=args.router_id or f"{socket.gethostname()}:{args.port}",
            lease_ttl_s=args.lease_ttl,
            lease_renew_interval_s=args.lease_renew_interval,
            lease_safety_margin_s=args.lease_safety_margin,
            deployment_status=args.deployment_status or None,
        )
    except (OSError, ValueError) as exc:
        return failure("narwhal-serve", f"configure router from {args.fleet}", exc, 2)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=int(
            args.graceful_timeout if args.graceful_timeout is not None else cfg.graceful_timeout_s
        ),
    )
    YieldingServer(config, drain_s=args.shutdown_drain).run()
    return 0


class YieldingServer(uvicorn.Server):
    """Hand router control to the standby once the router stops accepting connections."""

    def __init__(self, config: uvicorn.Config, *, drain_s: float = SHUTDOWN_DRAIN_S) -> None:
        super().__init__(config)
        self.drain_s = drain_s

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Run uvicorn's shutdown, releasing the lease before the open requests drain.

        The router first fails its readiness and fleet-state reads, so the load balancer's next
        check and the standby's probes fail, and serves every request for `drain_s` while the
        load balancer takes it out of rotation. Only then does it stop listening and close its
        idle connections. Closing either earlier resets a connection the load balancer has
        just opened or reused, which the client receives as HTTP 502. A request that reaches
        the router is admitted while it still holds the lease. The steps follow
        `uvicorn.Server.shutdown`.
        """
        state = getattr(self.config.app, "state", None)
        yield_control = getattr(state, "yield_control", None)
        if yield_control is None:
            await super().shutdown(sockets=sockets)
            return
        server_log.info("Shutting down")
        begin_shutdown = getattr(state, "begin_shutdown", None)
        if begin_shutdown is not None:
            begin_shutdown()
        await asyncio.sleep(self.drain_s)
        for server in self.servers:
            server.close()
        for sock in sockets or []:
            sock.close()
        for connection in list(self.server_state.connections):
            connection.shutdown()
        await asyncio.sleep(0.1)
        yield_control()
        try:
            await asyncio.wait_for(
                self._wait_tasks_to_complete(), timeout=self.config.timeout_graceful_shutdown
            )
        except TimeoutError:
            server_log.error(
                "Cancel %s running task(s), timeout graceful shutdown exceeded",
                len(self.server_state.tasks),
            )
            for task in self.server_state.tasks:
                task.cancel(msg="Task cancelled, timeout graceful shutdown exceeded")
        if not self.force_exit:
            await self.lifespan.shutdown()


def _port_in_use(port: int, host: str = "127.0.0.1") -> str | None:
    """Return the configured listener's bind failure, or None when it binds."""
    try:
        check_http_bind(host, port)
    except OSError as error:
        return str(error)
    return None


if __name__ == "__main__":
    raise SystemExit(serve())
