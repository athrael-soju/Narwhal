"""Optional stdio command with dependency-free help and version handling."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from narwhal.cli_support import add_version_argument


def main(argv: list[str] | None = None) -> int:
    """Validate local startup inputs and serve MCP until stdin closes."""
    parser = argparse.ArgumentParser(
        description="Serve Narwhal's implemented tools over MCP stdio."
    )
    add_version_argument(parser)
    parser.add_argument(
        "--registry",
        type=Path,
        help="management registry JSON path (default: NARWHAL_MANAGEMENT_REGISTRY)",
    )
    args = parser.parse_args(argv)
    try:
        from .server import serve
    except ImportError:
        print(
            "narwhal-mcp: install optional dependencies: pip install 'narwhal-inference[mcp]'",
            file=sys.stderr,
        )
        return 2
    selected = args.registry or os.environ.get("NARWHAL_MANAGEMENT_REGISTRY")
    if not selected:
        print("narwhal-mcp: --registry or NARWHAL_MANAGEMENT_REGISTRY is required", file=sys.stderr)
        return 2
    from narwhal.deployment.management_registry import load_registry

    try:
        registry = load_registry(Path(selected))
    except (OSError, ValueError) as exc:
        detail = (
            "document fields failed validation"
            if isinstance(exc, ValidationError)
            else type(exc).__name__
        )
        print(
            f"narwhal-mcp: registry rejected ({detail}); check its schema, ownership and mode 0600",
            file=sys.stderr,
        )
        return 2
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s: %(message)s")
    try:
        from narwhal.deployment.management_coordinator import OperationCoordinator

        from .inspection import inspection_adapters
        from .operations import operation_adapters

        coordinator = OperationCoordinator(
            registry, registry_path=Path(selected), entry_point="mcp"
        )
        coordinator.reconcile_startup()
        adapters = (*inspection_adapters(registry), *operation_adapters(registry, coordinator))
        asyncio.run(serve(adapters))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("narwhal-mcp: server failed", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
