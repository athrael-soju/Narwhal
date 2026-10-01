"""Shared options for installed Narwhal commands."""

from __future__ import annotations

import argparse
from importlib import metadata


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
