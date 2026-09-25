"""Offline validation and effective-configuration inspection commands."""

from __future__ import annotations

import argparse
import json
import sys

from ..command_results import invoke, json_mode, set_data, set_operation
from .inspection import inspect_config
from .model import FleetConfig


def main(argv: list[str] | None = None) -> int:
    """Run the installed offline configuration workflow."""
    return invoke("narwhal", argv, _main, operation="config", capture_child_stdout=False)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="narwhal config")
    configure_parser(parser)
    return run(parser.parse_args(argv))


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Register offline actions for the installed root and configuration help."""
    parser.description = (
        "Validate fleet files and inspect defaults with filesystem and environment reads."
    )
    parser.epilog = "Run narwhal-check --fleet PATH after profiling to check live fleet readiness."
    actions = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("validate", "Validate schema, fields, endpoint references and cross-field limits"),
        ("inspect", "Print the resolved fleet-file layer and default serving limits"),
    ):
        action = actions.add_parser(name, help=help_text, description=help_text)
        action.add_argument(
            "--fleet", required=True, help="Fleet JSON, relative to the working directory"
        )
        action.add_argument(
            "--format", choices=("text", "json"), default="text", help="Command result format"
        )


def run(args: argparse.Namespace) -> int:
    """Validate a parsed fleet-file action and publish its effective configuration."""
    set_operation(f"config {args.action}")
    try:
        config = FleetConfig.load(args.fleet)
    except (OSError, ValueError) as exc:
        if json_mode():
            raise
        print(str(exc), file=sys.stderr)
        return 2
    result = inspect_config(config, args.fleet)
    set_data(result)
    if json_mode():
        return 0
    if args.action == "inspect":
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(f"Validated {result['source']}: {len(config.engines)} engines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
