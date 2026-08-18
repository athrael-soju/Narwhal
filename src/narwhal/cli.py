"""Process entry points."""

from __future__ import annotations

import argparse
import logging
import socket
import sys

import httpx
import uvicorn

from .app import create_app
from .config import FleetConfig

# uvicorn's level names against the logging module's. uvicorn takes `trace`,
# which logging has no level for and which `logging.basicConfig` rejects with
# `ValueError: Unknown level: 'TRACE'`.
LOG_LEVELS = {
    "critical": "CRITICAL",
    "error": "ERROR",
    "warning": "WARNING",
    "info": "INFO",
    "debug": "DEBUG",
    "trace": "DEBUG",
}


def serve(argv: list[str] | None = None) -> int:
    """CLI: validate flags, refuse a taken port, hand the app to uvicorn."""
    ap = argparse.ArgumentParser(description="Run the Arrow router over a stateless fleet")
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
        "--journal-payloads",
        default="",
        metavar="PATH",
        help="also capture each request's prompt and output text to PATH "
        "(JSONL, joined to the journal by rid). Off by default: the journal "
        "records lengths and timings, never content",
    )
    ap.add_argument(
        "--journal-payloads-max-chars",
        type=int,
        default=None,
        help="truncate each captured field at this many characters (default: the config's, 2048)",
    )
    ap.add_argument(
        "--journal-payloads-max-mb",
        type=int,
        default=None,
        help="stop capturing when the payload file reaches this size (default: the config's, 256)",
    )
    ap.add_argument(
        "--standby-of",
        default="",
        metavar="URL",
        help="run as a warm standby of the primary router at URL: refuse "
        "traffic 503 while it answers, shadow its handoff document, take "
        "over when it goes silent",
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
        "--resume",
        action="store_true",
        help="take the last state handoff (roles, the breaker's holds, counters) "
        "instead of the fleet file's opening split",
    )
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if args.max_concurrent is not None and args.max_concurrent < 1:
        ap.error(f"--max-concurrent must be at least 1, got {args.max_concurrent}")
    # uvicorn counts the seconds forward from SIGTERM, so a negative one is a
    # shutdown that drops every in-flight stream and reports nothing.
    if args.graceful_timeout is not None and args.graceful_timeout < 0:
        ap.error(f"--graceful-timeout cannot be negative, got {args.graceful_timeout}")

    logging.basicConfig(
        level=LOG_LEVELS[args.log_level],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs one INFO line per engine leg, drowning the router's own
    # lines at any driven rate. Asking for debug asks for those lines, so
    # only debug keeps them.
    if LOG_LEVELS[args.log_level] != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if (holder := _port_in_use(args.port)) is not None:
        print(
            f"port {args.port} is already serving ({holder}). The fleet has one "
            f"router port, so starting here would leave the old router taking the "
            f"traffic while this one reports a failed bind to its log.",
            file=sys.stderr,
        )
        return 2

    cfg = FleetConfig.load(args.fleet)
    if args.resume:
        cfg.resume = True
    if args.standby_probe_interval is not None and args.standby_probe_interval <= 0:
        ap.error("--standby-probe-interval must be positive")
    if args.standby_takeover_after is not None and args.standby_takeover_after < 1:
        ap.error("--standby-takeover-after must be at least 1")
    uvicorn.run(
        create_app(
            cfg,
            max_concurrent=args.max_concurrent,
            journal_path=args.journal or None,
            payloads_path=args.journal_payloads or None,
            payloads_max_chars=args.journal_payloads_max_chars,
            payloads_max_mb=args.journal_payloads_max_mb,
            standby_of=args.standby_of.rstrip("/") or None,
            standby_probe_interval_s=args.standby_probe_interval,
            standby_takeover_after=args.standby_takeover_after,
        ),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=int(
            args.graceful_timeout if args.graceful_timeout is not None else cfg.graceful_timeout_s
        ),
    )
    return 0


def _port_in_use(port: int) -> str | None:
    """Describe what holds the port, or None if it is free."""
    for family, addr in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            try:
                s.connect((addr, port))
            except OSError:
                continue
        try:
            r = httpx.get(
                f"http://[{addr}]:{port}/health" if ":" in addr else f"http://{addr}:{port}/health",
                timeout=2.0,
            )
            return f"answering /health with {r.text[:60]}"
        except httpx.HTTPError:
            return "accepting connections"
    return None


if __name__ == "__main__":
    raise SystemExit(serve())
