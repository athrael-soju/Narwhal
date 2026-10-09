"""Start the fleet control service on a loopback listener."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from narwhal.runtime.listeners import check_http_bind

from .aiperf import runner_for, workload_routes
from .app import create_app
from .config import DEFAULT_CONFIG, ConfigError, load_config, read_token
from .console import PUBLIC_PATHS, console_routes
from .engines import EngineActions, engine_routes
from .overlays import Overlays, overlay_routes
from .service import ControlService


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
        check_http_bind(config.host, config.port)
    except (ConfigError, OSError) as exc:
        print(f"fleet control failed: {exc}", file=sys.stderr)
        return 2
    service = ControlService(config, runner=runner_for(config, os.environ))
    routers = [
        console_routes(config),
        engine_routes(EngineActions(service)),
        overlay_routes(Overlays(service)),
    ]
    if config.load is not None:
        routers.append(workload_routes(config.load))
    app = create_app(service, token, routers=routers, public=PUBLIC_PATHS)
    uvicorn.run(app, host=config.host, port=config.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
