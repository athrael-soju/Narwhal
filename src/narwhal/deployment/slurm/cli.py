"""narwhal-slurm: start a Narwhal fleet inside one Slurm allocation, one task per node."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ... import backends
from ...cli_support import add_version_argument
from .allocation import from_environment, resolve
from .launch import Launch, Settings, StageFailed, Stopped, check_fleet_setting

COMMAND = "narwhal-slurm"


def _pair(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key or "\n" in value:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, not {text!r}")
    return key, value


class _NonFinite(ValueError):
    pass


def _refuse_constant(name: str) -> Any:
    raise _NonFinite(name)


def _setting(text: str) -> tuple[str, Any]:
    path, sep, raw = text.partition("=")
    if not sep or not path:
        raise argparse.ArgumentTypeError(f"expected PATH=VALUE, not {text!r}")
    try:
        value = json.loads(raw, parse_constant=_refuse_constant)
    except _NonFinite as error:
        raise argparse.ArgumentTypeError(f"{path}: {error} is not a finite number") from error
    except ValueError:
        value = raw
    return path, value


def _positive(kind: Callable[[str], float]) -> Callable[[str], Any]:
    def parse(text: str) -> Any:
        value = kind(text)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"must be positive, not {text}")
        return value

    return parse


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=COMMAND,
        description="Start engines on every node of a Slurm allocation; on the head node, write "
        "the fleet file, profile the engines and start the router. Run one task per node: "
        "srun --ntasks-per-node=1 narwhal-slurm ...",
    )
    add_version_argument(p)
    p.add_argument("--model", required=True, help="model path or name the engines load")
    p.add_argument("--served-model-name", required=True, help="model name clients request")
    p.add_argument("--run-dir", required=True, type=Path, help="shared directory for run files")
    p.add_argument(
        "--slo-ttft-s", required=True, type=_positive(float), help="fleet TTFT SLO in seconds"
    )
    p.add_argument(
        "--slo-tpot-s", required=True, type=_positive(float), help="fleet TPOT SLO in seconds"
    )
    p.add_argument("--backend", default=backends.DEFAULT_BACKEND, help="engine backend")
    p.add_argument(
        "--engines-per-node", type=_positive(int), default=1, help="engines on each node"
    )
    p.add_argument("--tensor-parallel", type=_positive(int), default=1, help="devices per engine")
    p.add_argument("--prefill", type=_positive(int), help="engines that open as prefill (half)")
    p.add_argument("--engine-port", type=_positive(int), default=8100, help="first engine port")
    p.add_argument(
        "--transfer-port", type=_positive(int), default=5600, help="first KV side-channel port"
    )
    p.add_argument("--max-sequences", type=_positive(int), help="each engine's sequence limit")
    p.add_argument(
        "--engine-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra engine argument, repeatable; write options as --engine-arg=--flag=value",
    )
    p.add_argument(
        "--engine-env",
        action="append",
        default=[],
        type=_pair,
        metavar="NAME=VALUE",
        help="engine environment variable, repeatable, such as transport and device settings",
    )
    p.add_argument("--engine-python", default="python3", help="the engine image's Python")
    p.add_argument(
        "--device-env",
        default="CUDA_VISIBLE_DEVICES",
        help="variable that lists a node's devices and selects each engine's devices",
    )
    p.add_argument("--router-host", default="0.0.0.0", help="router bind address")
    p.add_argument("--router-port", type=_positive(int), default=8000, help="router port")
    p.add_argument(
        "--profile-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra narwhal-profile argument, repeatable",
    )
    p.add_argument(
        "--fleet-setting",
        action="append",
        default=[],
        type=_setting,
        metavar="PATH=VALUE",
        help="fleet field by dotted path, repeatable, such as serving.admission=open; "
        "VALUE is read as JSON, or else as a string",
    )
    p.add_argument(
        "--health-timeout-s",
        type=_positive(float),
        default=1800.0,
        help="seconds for every engine to answer /health",
    )
    p.add_argument(
        "--ready-timeout-s",
        type=_positive(float),
        default=600.0,
        help="seconds for the router to answer /ready",
    )
    p.add_argument(
        "--stop-grace-s",
        type=_positive(float),
        default=30.0,
        help="seconds each process has after SIGTERM before SIGKILL",
    )
    return p


def settings_from(args: argparse.Namespace) -> Settings:
    env = dict(args.engine_env)
    if args.device_env in env:
        raise ValueError(f"--engine-env must not set {args.device_env}; the launch sets it")
    paths = [path for path, _ in args.fleet_setting]
    for path in paths:
        check_fleet_setting(path)
    for path in paths:
        # A setting inside another would be replaced by it, or replace part of it.
        for other in paths:
            if other is not path and (other == path or other.startswith(f"{path}.")):
                raise ValueError(f"fleet setting {other!r} is also set by {path!r}")
    fleet_settings = dict(args.fleet_setting)
    return Settings(
        model=args.model,
        served_name=args.served_model_name,
        run_root=args.run_dir.resolve(),
        slo_ttft_s=args.slo_ttft_s,
        slo_tpot_s=args.slo_tpot_s,
        backend=args.backend,
        engines_per_node=args.engines_per_node,
        tensor_parallel=args.tensor_parallel,
        prefill=args.prefill,
        engine_port=args.engine_port,
        transfer_port=args.transfer_port,
        max_sequences=args.max_sequences,
        engine_args=tuple(args.engine_arg),
        engine_env=env,
        engine_python=args.engine_python,
        device_env=args.device_env,
        router_host=args.router_host,
        router_port=args.router_port,
        profile_args=tuple(args.profile_arg),
        fleet_settings=fleet_settings,
        health_timeout_s=args.health_timeout_s,
        ready_timeout_s=args.ready_timeout_s,
        stop_grace_s=args.stop_grace_s,
    )


def main(argv: list[str] | None = None, stop: threading.Event | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = settings_from(args)
        start = backends.load(settings.backend).allocation_start()
        allocation = from_environment(os.environ, settings.device_env)
        addresses = [resolve(node) for node in allocation.nodes]
        stop = stop or threading.Event()
        launch = Launch(settings, allocation, start, addresses, stop=stop)
    except ValueError as error:
        print(f"{COMMAND}: allocation: {error}", file=sys.stderr)
        return 2
    try:
        # Every node checks the fleet file before any engine starts.
        launch.check_fleet()
    except ValueError as error:
        print(f"{COMMAND}: fleet: {error}", file=sys.stderr)
        return 2
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, lambda *_: stop.set())
    role = "head" if allocation.head else "worker"
    print(
        f"{COMMAND}: allocation: job {allocation.job}, node {allocation.rank} of "
        f"{len(allocation.nodes)} ({role}), run directory {launch.directory}",
        flush=True,
    )
    try:
        launch.run()
    except StageFailed as error:
        print(f"{COMMAND}: {error.stage}: {error.detail}", file=sys.stderr)
        return 1
    except Stopped:
        print(f"{COMMAND}: stop: stopped", flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
