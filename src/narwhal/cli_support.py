"""Shared options for installed Narwhal commands."""

from __future__ import annotations

import argparse
import json
import os
from importlib import metadata

from .observability.tools import METRICS_URL_ENV, listen_address


def _commit() -> str:
    """The Git commit pip recorded for an install from a repository, as ` (abc12345)`."""
    try:
        recorded = metadata.distribution("narwhal-inference").read_text("direct_url.json")
        commit = json.loads(recorded or "{}").get("vcs_info", {}).get("commit_id")
    except (metadata.PackageNotFoundError, OSError, ValueError, AttributeError):
        return ""
    return f" ({commit[:8]})" if isinstance(commit, str) and commit else ""


def add_version_argument(parser: argparse.ArgumentParser) -> None:
    """Add `--version`, reporting this environment's narwhal-inference distribution version."""
    try:
        version = metadata.version("narwhal-inference")
        version += _commit()
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
        help="serve the operation, its engines and its progress at URL/metrics, where URL is "
        f"http://host:port, while the operation runs (default: ${METRICS_URL_ENV}, else no "
        "endpoint)",
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
