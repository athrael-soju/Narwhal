"""Shared options for installed Narwhal commands."""

from __future__ import annotations

import argparse
import os
from importlib import metadata

from .observability.tools import METRICS_URL_ENV, listen_address


def add_version_argument(parser: argparse.ArgumentParser) -> None:
    """Add `--version`, reporting this environment's narwhal-inference distribution version."""
    try:
        version = metadata.version("narwhal-inference")
    except metadata.PackageNotFoundError:
        version = "unknown (distribution metadata unavailable)"
    parser.add_argument(
        "--version",
        action="version",
        version=f"narwhal-inference {version}",
        help="print this environment's Narwhal distribution version and exit",
    )


def add_tool_metrics_argument(parser: argparse.ArgumentParser) -> None:
    """Add `--metrics-url`, where an operation that sends requests to engines serves metrics."""
    parser.add_argument(
        "--metrics-url",
        help="serve the operation, its engines and its progress at this http://host:port "
        f"/metrics while the operation runs (default: ${METRICS_URL_ENV}, else no endpoint)",
    )


def tool_metrics_url(parser: argparse.ArgumentParser, url: str | None) -> str | None:
    """Return `--metrics-url`, else the environment's URL; exit with a usage error naming the
    source when the URL is not an http://host:port origin."""
    source = "--metrics-url"
    if url is None:
        url, source = os.environ.get(METRICS_URL_ENV) or None, METRICS_URL_ENV
    if url:
        try:
            listen_address(url, source)
        except ValueError as exc:
            parser.error(str(exc))
    return url
