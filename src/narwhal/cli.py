"""Process entry points."""

from __future__ import annotations

import argparse
import logging
import math
import socket
import sys

import uvicorn

from .cli_support import add_version_argument
from .config import FleetConfig
from .runtime.listeners import check_http_bind
from .serving.app import create_app

# Map uvicorn's trace level to logging's DEBUG.
LOG_LEVELS = {
    "critical": "CRITICAL",
    "error": "ERROR",
    "warning": "WARNING",
    "info": "INFO",
    "debug": "DEBUG",
    "trace": "DEBUG",
}


def serve(argv: list[str] | None = None) -> int:
    """Run the serving CLI."""
    ap = argparse.ArgumentParser(description="Run Narwhal over a disaggregated engine fleet")
    add_version_argument(ap)
    ap.add_argument("--fleet", required=True, help="fleet config JSON")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log-level", default="info", choices=sorted(LOG_LEVELS))
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
        help="seconds for in-flight requests to finish on shutdown "
        "(default: the config's graceful_timeout_s)",
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
        "--standby-probe-interval",
        type=float,
        default=None,
        help="seconds between standby probes of the primary (default 0.25)",
    )
    ap.add_argument(
        "--standby-takeover-after",
        type=int,
        default=None,
        help="consecutive failed probes before the standby takes over (default 4)",
    )
    ap.add_argument(
        "--standby-max-handoff-age",
        type=float,
        default=30.0,
        help="maximum age of state eligible for automatic takeover (default 30)",
    )
    ap.add_argument(
        "--lease-path",
        default="",
        help="lease JSON on storage shared by the primary and standby",
    )
    ap.add_argument(
        "--router-id",
        default="",
        help="stable router name reported with its unique lease holder token",
    )
    ap.add_argument("--lease-ttl", type=float, default=5.0, help="lease lifetime in seconds")
    ap.add_argument(
        "--lease-renew-interval",
        type=float,
        default=1.0,
        help="seconds between lease renewals",
    )
    ap.add_argument(
        "--lease-safety-margin",
        type=float,
        default=1.0,
        help="maximum relative clock skew reserved before expiry (default 1)",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="restore roles, breaker holds, and counters from the last state handoff",
    )
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if args.max_concurrent is not None and args.max_concurrent < 1:
        ap.error(f"--max-concurrent must be at least 1, got {args.max_concurrent}")
    # uvicorn counts this forward from SIGTERM.
    if args.graceful_timeout is not None and args.graceful_timeout < 0:
        ap.error(f"--graceful-timeout cannot be negative, got {args.graceful_timeout}")

    logging.basicConfig(
        level=LOG_LEVELS[args.log_level],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx emits one INFO line per engine leg. Keep it only for debug runs.
    if LOG_LEVELS[args.log_level] != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if (error := _port_in_use(args.port, args.host)) is not None:
        print(
            f"Cannot bind {args.host}:{args.port}: {error}",
            file=sys.stderr,
        )
        return 2

    cfg = FleetConfig.load(args.fleet)
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
            router_id=args.router_id or f"{socket.gethostname()}:{args.port}",
            lease_ttl_s=args.lease_ttl,
            lease_renew_interval_s=args.lease_renew_interval,
            lease_safety_margin_s=args.lease_safety_margin,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=int(
            args.graceful_timeout if args.graceful_timeout is not None else cfg.graceful_timeout_s
        ),
    )
    return 0


def _port_in_use(port: int, host: str = "127.0.0.1") -> str | None:
    """Return the configured listener's bind failure, or None when it binds."""
    try:
        check_http_bind(host, port)
    except OSError as error:
        return str(error)
    return None


if __name__ == "__main__":
    raise SystemExit(serve())
