"""Start the fleet control service on a loopback listener."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from narwhal.runtime.listeners import check_http_bind
from narwhal.serving.control_token import read_token as read_router_token

from .aiperf import runner_for, workload_routes
from .app import create_app
from .config import DEFAULT_CONFIG, ConfigError, load_config, read_token
from .console import PUBLIC_PATHS, console_routes
from .engines import EngineActions, engine_routes
from .live import live_routes
from .overlays import Overlays, overlay_routes
from .service import ControlService
from .signals import FleetSignals, signal_routes


def main(argv: Sequence[str] | None = None) -> int:
    """Load the private configuration and serve the control API until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("NARWHAL_CONTROL_CONFIG", DEFAULT_CONFIG)),
        help="private control configuration (default: NARWHAL_CONTROL_CONFIG or %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="uvicorn logging threshold (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        token = read_token(config, os.environ)
        router_token = read_router_token(os.environ)
        if router_token == token:
            raise ConfigError(
                f"{config.token_env} and NARWHAL_ROUTER_TOKEN must hold different tokens: "
                "the service's token reaches browsers and Prometheus"
            )
        check_http_bind(config.host, config.port)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"fleet control failed: {exc}", file=sys.stderr)
        return 2
    service = ControlService(config, runner=runner_for(config, os.environ))
    engines = EngineActions(service, router_token=router_token)
    routers = [
        console_routes(config, token),
        engine_routes(engines),
        overlay_routes(Overlays(service, engines=engines)),
        signal_routes(FleetSignals(service, engines=engines)),
    ]
    if config.load is not None:
        routers.append(workload_routes(config.load))
        routers.append(live_routes(service, config.load.goodput))
    app = create_app(service, token, routers=routers, public=PUBLIC_PATHS)
    uvicorn.run(app, host=config.host, port=config.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
