"""`narwhal deploy`: discover, reach, package and install a fleet on SSH hosts, or apply and
follow a Kubernetes cluster's `NarwhalFleet`."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import command_results as results
from ..cli_errors import failure
from ..observability.stack.start.stack import compose_problem
from . import (
    cache_groups,
    discovery,
    fabric,
    fleet_resource,
    install,
    kubernetes_cli,
    kubernetes_target,
    remote,
    run_record,
    supervision,
)
from . import deploy as stage_run
from . import trial as capacity
from .deploy_graph import DEPLOY_GRAPH
from .hosts import SSH, Host, RemoteError, RemoteTimeout, SignalStop, load_hosts
from .launch_engine.evidence import CheckFailed
from .runner import (
    HelpersSurvived,
    RunCancelled,
    RunLocked,
    StageFailed,
    check_directory,
    stale_notice,
)

# The stages `narwhal deploy run --until` can stop after, the capacity trial included. Every
# Kubernetes stage is named as the SSH stage it corresponds to.
RUN_STAGES = DEPLOY_GRAPH.order()[: DEPLOY_GRAPH.order().index(stage_run.TRIAL_STAGE) + 1]
# Each target's inputs, deployment and recovery pages.
# The SSH target runs the stage graph in this process; on Kubernetes the fleet controller runs
# it, and the subcommands drive the NarwhalFleet through `kubernetes_cli`.
TARGETS: dict[str, Any] = {"ssh": stage_run}

DESCRIPTIONS = {
    "plan": "Check every deployment input and each host's systemd user manager, then print "
    "each stage up to the monitoring stack, or the capacity trial with --trial, and whether run "
    "would run or skip it, and why.",
    "run": "Run the stages from host access to a ready router pair in the run directory, and "
    "with --trial the capacity trial, skipping each stage whose recorded result still holds, and "
    "stop at the first stage that fails.",
    "status": "Print each stage's state and evidence from the run record, and check whether "
    "each engine, sidecar, router and container the run started still runs or has stopped.",
    "down": "Stop the monitoring stack, load balancer, routers, sidecars and engines the run "
    "started, each only while its recorded identity still matches, and print each as stopped "
    "or not running.",
    "discover": "Log in to every host named in the loaded environment with its pinned host key, "
    "inspect GPUs, the engine image and the checkpoint, and write the host inventory, launch "
    "records, fleet and deployment.env.",
    "access": "Check the inventory, then log in once to each host with its pinned host key and "
    "record its hostname in a private log.",
    "package": "Check the Narwhal wheel against its SHA-256 and write it, the helper snapshots "
    "and each host's role files into a fresh package directory with a private manifest.",
    "install": "Copy a package to each selected host, checking every file's SHA-256, and "
    "install the wheel into the host's deployment directory.",
    "transfer-measure": "Measure the KV handoffs of every engine whose backend sizes its "
    "fabric budget from them, each to the next idle engine with the same launch inputs.",
    "cache-groups": "Group the engine roles of the launch records into cache-equivalence "
    "groups by image, model, allocation, runtime and transport, and by each engine's captured "
    "cache layout or measured KV handoff, and write cache-groups.json.",
    "fabric": "Size each cache group's KV budget and qualify every directed edge between "
    "engine hosts against it with iperf3 or ib_write_bw, reusing a retained sample whose link "
    "record is unchanged.",
    "shell": "Open an interactive shell on a role's host, in its deployment directory with the "
    "role environment loaded when --run names a package.",
    "tunnel": "Forward workstation loopback ports to a role's host until interrupted.",
}


def forward_ports(value: str) -> tuple[int, int]:
    """Parse LOCAL_PORT:REMOTE_PORT into a port pair."""
    if not re.fullmatch(r"[0-9]{1,5}:[0-9]{1,5}", value):
        raise argparse.ArgumentTypeError("Use LOCAL_PORT:REMOTE_PORT")
    local, remote = map(int, value.split(":"))
    if not all(1 <= port <= 65535 for port in (local, remote)):
        raise argparse.ArgumentTypeError("Ports must be between 1 and 65535")
    return local, remote


def _seconds(value: str) -> float:
    """Parse a positive number of seconds."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a number of seconds") from None
    if not 0 < seconds < 10**7:
        raise argparse.ArgumentTypeError("Use a positive number of seconds")
    return seconds


def role_file(value: str) -> tuple[str, Path]:
    """Parse ROLE=PATH into an engine role and a file path."""
    role, separator, path = value.partition("=")
    if not separator or not re.fullmatch(r"engine-[1-9][0-9]*", role) or not path:
        raise argparse.ArgumentTypeError("Use ROLE=PATH with an engine-<n> role")
    return role, Path(path)


def add_commands(commands: Any) -> None:
    """Register `deploy` beneath the installed root command."""
    deploy = commands.add_parser(
        "deploy",
        help="Deploy a fleet on SSH hosts, or run one of its stages alone",
        description="Deploy a fleet on SSH hosts. plan checks the inputs and prints what run "
        "would do, run takes the hosts to a ready router pair and resumes at the stage that "
        "did not "
        "pass, status reports the run and down stops what it started. discover inspects the "
        "hosts and writes the generated configuration, access checks every login, package "
        "writes the wheel and role files, install copies and installs them, transfer-measure "
        "measures KV handoffs, cache-groups groups the engines that share a fabric budget, "
        "fabric qualifies every directed edge between engine hosts, shell opens a role shell "
        "and tunnel forwards router ports to the workstation.",
        epilog="Documentation: https://athrael-soju.github.io/Narwhal/cli/Deploy/",
    )
    actions = deploy.add_subparsers(dest="action", required=True)
    for name, description in DESCRIPTIONS.items():
        action = actions.add_parser(name, help=description, description=description)
        if name not in ("shell", "tunnel"):
            results.add_format(action)
        if name in ("plan", "run", "status", "down"):
            action.add_argument(
                "--run",
                type=Path,
                required=True,
                help="run directory that holds the run record; run creates it",
            )
            action.add_argument(
                "--target",
                choices=("ssh", "kubernetes"),
                default="ssh",
                help="where the fleet runs: SSH hosts, or a Kubernetes cluster, whose "
                "NarwhalFleet the run applies and whose controller it follows (default: "
                "%(default)s)",
            )
            if name in ("run", "down"):
                action.add_argument(
                    "--timeout",
                    type=_seconds,
                    metavar="SECONDS",
                    help="with --target kubernetes, how long to follow the fleet until it is "
                    f"ready, or for down stopped (default: {kubernetes_cli.FOLLOW_SECONDS} for "
                    f"run, {kubernetes_cli.STOP_SECONDS} for down)",
                )
            if name == "down":
                action.add_argument(
                    "--delete",
                    action="store_true",
                    help="with --target kubernetes, delete the NarwhalFleet once it stopped",
                )
            if name in ("plan", "run"):
                action.add_argument(
                    "--until",
                    choices=RUN_STAGES,
                    metavar="STAGE",
                    help=f"stop after this stage (default: {stage_run.LAST_STAGE}, "
                    f"{stage_run.TRIAL_STAGE} with --trial, or "
                    f"{kubernetes_target.LAST_STAGE} with --target kubernetes)",
                )
                action.add_argument(
                    "--trial",
                    type=Path,
                    metavar="WORKLOAD",
                    help="narwhal.capacity-trial file: run the capacity trial of this workload "
                    "against its thresholds after the monitoring stack, on SSH hosts",
                )
            continue
        if name not in ("discover", "cache-groups"):
            action.add_argument(
                "--hosts",
                type=Path,
                help="host inventory (default: NARWHAL_HOSTS, else config/hosts.local.json)",
            )
        if name in ("discover", "package", "transfer-measure", "fabric"):
            action.add_argument(
                "--out", type=Path, required=True, help="fresh private output directory"
            )
        if name == "cache-groups":
            action.add_argument(
                "--out",
                type=Path,
                required=True,
                help="evidence directory for cache-groups.json, created when absent",
            )
            action.add_argument(
                "--run",
                type=Path,
                required=True,
                help="package directory that package wrote from the launch records",
            )
            action.add_argument(
                "--cache-layout",
                type=role_file,
                action="append",
                default=[],
                metavar="ROLE=PATH",
                help="an engine role's captured cache-layout.json; repeat for every engine "
                "that captures its cache layout",
            )
            action.add_argument(
                "--transfer-record",
                type=role_file,
                action="append",
                default=[],
                metavar="ROLE=PATH",
                help="an engine role's measured transfer.json; repeat for every engine whose "
                "KV handoffs are measured",
            )
        if name in ("transfer-measure", "fabric"):
            action.add_argument(
                "--run", type=Path, required=True, help="installed package directory"
            )
        if name == "fabric":
            action.add_argument(
                "--groups",
                type=Path,
                required=True,
                help="cache-groups.json that cache-groups derived from this package",
            )
            action.add_argument(
                "--retained",
                type=Path,
                help="earlier fabric output whose samples to reuse while their links hold",
            )
        if name == "install":
            action.add_argument(
                "--run", type=Path, required=True, help="package directory that package wrote"
            )
            selection = action.add_mutually_exclusive_group()
            selection.add_argument(
                "--host", action="append", help="install one inventory host; repeatable"
            )
            selection.add_argument(
                "--role",
                action="append",
                help="install the host of a router or engine-<n> role; repeatable",
            )
        if name == "shell":
            action.add_argument(
                "--role", required=True, help="router or engine-<n> role whose host to open"
            )
            action.add_argument(
                "--run", type=Path, help="installed package whose role environment to load"
            )
        if name == "tunnel":
            action.add_argument("--role", default="router", help="role whose host to forward to")
            action.add_argument(
                "--forward",
                type=forward_ports,
                action="append",
                required=True,
                metavar="LOCAL_PORT:REMOTE_PORT",
                help="forward workstation 127.0.0.1:LOCAL_PORT to REMOTE_PORT; repeatable",
            )
            action.add_argument(
                "--remote-address",
                type=ipaddress.ip_address,
                default="127.0.0.1",
                help="address the forwarded ports listen on at the host (default: %(default)s)",
            )


def _hosts(args: argparse.Namespace, env: dict[str, str]) -> list[Host]:
    path = args.hosts or Path(env.get("NARWHAL_HOSTS", "config/hosts.local.json"))
    return load_hosts(path, env)


def _role_host(hosts: list[Host], role: str) -> Host:
    host = next((h for h in hosts if role in h.roles), None)
    if host is None:
        raise ValueError(f"Select a role from the inventory, not {role}")
    return host


def _ssh(env: dict[str, str], logs: Path) -> SSH:
    ssh = SSH(env, logs)
    print(f"Private command logs: {logs}", flush=True)
    return ssh


def _access_logs() -> Path:
    return Path("runs") / f"access-{uuid.uuid4().hex}"


def _discover(args: argparse.Namespace, env: dict[str, str]) -> int:
    fleet = Path(env.get("NARWHAL_FLEET", "config/fleet.json"))
    results.add_artifact("deployment_env", fleet.with_name("deployment.env"))
    derived = discovery.discover(env, args.out)
    results.set_data({"out": str(args.out), "deployment_env": str(derived)})
    print(f"Generated private configuration. Load {derived} before packaging.")
    return 0


def _cache_groups(args: argparse.Namespace, env: dict[str, str]) -> int:
    launch_config = Path(env.get("NARWHAL_LAUNCH_CONFIG", "config/engine-launch.local.json"))
    evidence: dict[str, dict[str, Path]] = {}
    for option, pairs in (
        ("--cache-layout", args.cache_layout),
        ("--transfer-record", args.transfer_record),
    ):
        roles = [role for role, _ in pairs]
        if len(set(roles)) != len(roles):
            raise ValueError(f"Give one {option} for each role")
        evidence[option] = dict(pairs)
    results.add_artifact("cache_groups", args.out / cache_groups.EVIDENCE)
    document = cache_groups.derive(
        launch_config,
        evidence["--cache-layout"],
        evidence["--transfer-record"],
        args.run,
        args.out,
    )
    groups = [
        {key: group[key] for key in ("id", "representative", "roles")}
        for group in document["groups"]
    ]
    results.set_data({"out": str(args.out), "groups": groups})
    for group in groups:
        members = ",".join(group["roles"])
        print(f"{group['id']}: representative={group['representative']}; roles={members}")
    return 0


def _access(hosts: list[Host], env: dict[str, str]) -> int:
    logs = _access_logs()
    ssh = _ssh(env, logs)
    for host in hosts:
        ssh.run(host, "access", "management access", "hostname")
        print(f"{host.id}: {', '.join(host.roles)}; host key matched, SSH login verified")
    results.set_data(
        {"logs": str(logs), "hosts": [{"id": h.id, "roles": list(h.roles)} for h in hosts]}
    )
    return 0


def _package(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    results.add_artifact("manifest", args.out / "manifest.json")
    install.package(hosts, env, args.out.resolve())
    results.set_data({"run": str(args.out), "hosts": [host.id for host in hosts]})
    print(f"Packaged {len(hosts)} hosts in {args.out}; private manifest and inputs ready")
    return 0


def _install(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    manifest = install.load_run(args.run, hosts, env)
    if args.role:
        owners = {role: h.id for h in hosts for role in h.roles}
        if set(args.role) - owners.keys():
            raise ValueError("Select --role values from the inventory")
        chosen = {owners[role] for role in args.role}
    else:
        chosen = set(args.host or [host.id for host in hosts])
    if chosen - {host.id for host in hosts}:
        raise ValueError("Select --host values from the inventory")
    selected = [h for h in hosts if h.id in chosen]
    install.install(selected, manifest, args.run, _ssh(env, args.run / "logs"))
    results.set_data({"run": str(args.run), "hosts": [host.id for host in selected]})
    return 0


def _transfer_measure(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    manifest = install.load_run(args.run, hosts, env)
    engines, peers, prompt_tokens = fabric.measure_handoffs(hosts, manifest, args.run, env)
    args.out.mkdir(mode=0o700, parents=True)
    results.add_artifact("transfer-measure", args.out / "transfer-measure.json")
    ssh = _ssh(env, args.out / "logs")
    summary = fabric.measure(engines, peers, prompt_tokens, manifest["remote_dir"], ssh, args.out)
    results.set_data({"out": str(args.out), **summary})
    print(f"Measured the handoffs of {len(peers)} engines; records in {args.out}")
    return 0


def _fabric(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    manifest = install.load_run(args.run, hosts, env)
    if args.retained is not None and not (args.retained / "fabric.json").is_file():
        raise ValueError(f"--retained {args.retained} holds no fabric.json; name an earlier --out")
    plan = fabric.load(hosts, manifest, args.run, env, args.groups)
    args.out.mkdir(mode=0o700, parents=True)
    results.add_artifact("fabric", args.out / "fabric.json")
    ssh = _ssh(env, args.out / "logs")
    try:
        summary = fabric.qualify(plan, ssh, args.out, args.retained)
    except fabric.BelowBudget as exc:
        results.set_data({"out": str(args.out)})
        results.set_status("failed_gate")
        results.record_error("gate_failed", str(exc), stage=exc.stage, context=exc.context)
        if not results.json_mode():
            print(f"narwhal: deploy fabric: {exc}", file=sys.stderr)
        return 1
    results.set_data({"out": str(args.out), **summary})
    print(f"Qualified {len(summary['edges'])} directed samples; evidence in {args.out}")
    return 0


def _shell(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    host = _role_host(hosts, args.role)
    if args.run:
        manifest = install.load_run(args.run, hosts, env)
        _ssh(env, args.run / "logs").shell(host, install.shell_script(manifest, args.role))
    else:
        _ssh(env, _access_logs()).shell(host, "exec bash -l")
    return 0


def _tunnel(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    host = _role_host(hosts, args.role)
    if len({local for local, _ in args.forward}) != len(args.forward):
        raise ValueError("Select a distinct local port for each forward")
    address = str(args.remote_address)
    if args.remote_address.version == 6:
        address = f"[{address}]"
    forwards = [f"127.0.0.1:{local}:{address}:{remote}" for local, remote in args.forward]
    ssh = _ssh(env, _access_logs())
    print(
        "Keep this terminal open; Ctrl-C closes its forwards. SSH errors appear here.", flush=True
    )
    try:
        ssh.tunnel(host, forwards)
    except SignalStop as stop:
        return 128 + stop.signum
    except KeyboardInterrupt:
        return 130
    return 0


def _check_trial(args: argparse.Namespace) -> None:
    """Refuse a trial on Kubernetes, and a run to the trial stage without a trial."""
    if args.trial is not None and args.target != "ssh":
        raise ValueError("--trial runs the capacity trial on SSH hosts only")
    if args.until == stage_run.TRIAL_STAGE and args.trial is None:
        raise ValueError("--until trial needs --trial with the trial's workload file")


def _deployment(args: argparse.Namespace, env: dict[str, str], *, dry: bool = False) -> Any:
    """The target's deployment of the run directory; on SSH hosts it carries `--trial`."""
    directory = args.run.resolve()
    if args.target == "ssh":
        trial = getattr(args, "trial_snapshot", None)
        return stage_run.Deployment(directory, env, dry=dry, trial=trial)
    return TARGETS[args.target].Deployment(directory, env, dry=dry)


def _inputs(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any] | None:
    """The run's inputs, or None after reporting every missing or malformed one."""
    try:
        if args.target == "ssh":
            # The trial's bytes are read once: the trial input is their digest, and the trial
            # stage runs from them.
            try:
                args.trial_snapshot = capacity.snapshot(args.trial) if args.trial else None
            except ValueError as error:
                raise stage_run.InputError([str(error)]) from None
            inputs: dict[str, Any] = stage_run.inputs(env, args.trial_snapshot)
        else:
            inputs = TARGETS[args.target].inputs(env)
        return inputs
    except stage_run.InputError as error:
        for problem in error.problems:
            results.record_error("invalid_input", problem, stage="inputs")
            print(f"narwhal: deploy: {problem}", file=sys.stderr)
        results.set_status("invalid_input")
        return None


def _decisions(decisions: Any) -> list[dict[str, Any]]:
    rows = []
    for decision in decisions:
        row = {"stage": decision.key, "runs": decision.ran, "reasons": list(decision.reasons)}
        if decision.unobserved:
            row["unobserved"] = list(decision.unobserved)
        rows.append(row)
        if decision.ran:
            print(f"{decision.key}: runs; {'; '.join(decision.reasons)}")
        elif decision.unobserved:
            facts = ", ".join(decision.unobserved)
            print(f"{decision.key}: skipped unless run finds a change; {facts} not observed")
        else:
            print(f"{decision.key}: skipped; its recorded result holds")
    return rows


def _plan(args: argparse.Namespace, env: dict[str, str]) -> int:
    _check_trial(args)
    inputs = _inputs(args, env)
    if inputs is None:
        return 2
    problems = {}
    # A cluster runs no systemd units; access checks the cluster when `run` starts.
    if args.target == "ssh":
        ssh = SSH(env, _access_logs())
        for host in discovery.derive_hosts(env):
            runner = remote.SSHRunner(ssh, host, "access")
            found = supervision.check_systemd_host(runner)
            # The primary router's host runs the monitoring stack with Docker Compose.
            if "router" in host.roles and (problem := compose_problem(runner)):
                found.append(problem)
            if found:
                problems[host.id] = found
    directory = args.run.resolve()
    deployment = _deployment(args, env, dry=True)
    result = deployment.runner().plan(inputs, until=_until(args))
    rows = _decisions(result.decisions)
    notes = [*_topology_notes(args, env), *result.notes]
    for note in notes:
        print(f"Note: {note}")
    results.set_data({"run": str(directory), "stages": rows, "hosts": problems, "notes": notes})
    if problems:
        for name, found in problems.items():
            for problem in found:
                message = f"{name}: {problem}"
                results.record_error("gate_failed", message, stage="plan")
                print(f"narwhal: deploy plan: {message}", file=sys.stderr)
        results.set_status("failed_gate")
        return 1
    return 0


def _stage_failure(error: StageFailed) -> tuple[str, str, int]:
    """The command result of a failed stage: a rejected check is a failed gate.

    Host commands follow the command result contract, so a host step that exits 1 was
    rejected by its gate. Any other failure stopped the stage before it could decide.
    """
    if error.record["status"] == run_record.TIMEOUT:
        return "error", "stage_timeout", 4
    cause = error.__cause__
    if isinstance(cause, (fabric.BelowBudget, CheckFailed)) or (
        isinstance(cause, remote.StepFailed) and cause.context["exit_status"] == 1
    ):
        return "failed_gate", "gate_failed", 1
    return "error", "operation_failed", 4


def _topology_notes(args: argparse.Namespace, env: dict[str, str]) -> list[str]:
    """What the inventory leaves out that an operator should know before a run."""
    if args.target == "ssh" and not stage_run.paired(env):
        return [stage_run.SINGLE_ROUTER]
    return []


def _until(args: argparse.Namespace) -> str:
    """The last stage to plan or run: `--until`, else the trial with `--trial`, else the
    target's last stage."""
    if args.until:
        return str(args.until)
    if args.trial:
        return stage_run.TRIAL_STAGE
    if args.target == "kubernetes":
        return kubernetes_target.LAST_STAGE
    return str(TARGETS[args.target].LAST_STAGE)


def _trial_result(directory: Path) -> dict[str, Any] | None:
    """The trial result of the run's last trial attempt, when it wrote one."""
    entry = run_record.read(directory)["stages"].get(stage_run.TRIAL_STAGE)
    path = directory / entry["directory"] / "trial-result.json" if entry else None
    if path is None or not path.is_file():
        return None
    result: dict[str, Any] = json.loads(path.read_text())
    return result


def _publish(deployment: Any) -> None:
    """Give the routers the run's status; a missed upload is a warning, not a failure. A target
    without router hosts to copy it to publishes nothing."""
    publish = getattr(deployment, "publish_status", None)
    for warning in publish() if publish else ():
        print(f"narwhal: deploy run: warning: {warning}", file=sys.stderr)


def _run(args: argparse.Namespace, env: dict[str, str]) -> int:
    _check_trial(args)
    inputs = _inputs(args, env)
    if inputs is None:
        return 2
    directory = args.run.resolve()
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    results.add_artifact("run_record", directory / "run.json")
    target = TARGETS[args.target]
    deployment = _deployment(args, env)
    runner = deployment.runner()
    for note in _topology_notes(args, env):
        print(f"Note: {note}")

    def stale(found: Mapping[str, Any] | None) -> dict[str, Any]:
        """Print a stale lock the run found, and return the result data that carries it."""
        if found is None:
            return {}
        print(f"narwhal: deploy run: {stale_notice(found)}", file=sys.stderr)
        return {"stale_lock": dict(found)}

    try:
        result = runner.run(inputs, until=_until(args))
    except StageFailed as error:
        failure = error.record["failure"]
        recovery = target.recovery_url(failure["recovery"])
        status, code, exit_code = _stage_failure(error)
        failed: dict[str, Any] = {"run": str(directory), "failed_stage": error.key}
        if error.key == stage_run.TRIAL_STAGE and (found := _trial_result(directory)):
            failed["trial"] = found
        results.set_data({**failed, **stale(error.stale_lock)})
        results.record_error(
            code,
            failure["message"],
            stage=error.key,
            context={**failure["context"], "recovery": recovery, "status": error.record["status"]},
        )
        results.set_status(status)
        print(
            f"narwhal: deploy run: {error.key} {error.record['status']}: {failure['message']}",
            file=sys.stderr,
        )
        print(f"Recovery: {recovery}", file=sys.stderr)
        print(f"Evidence: {directory / error.record['directory']}", file=sys.stderr)
        _publish(deployment)
        return exit_code
    except RunCancelled as error:
        results.set_data(
            {"run": str(directory), "cancelled_stage": error.key, **stale(error.stale_lock)}
        )
        results.record_error("stage_cancelled", str(error), stage=error.key)
        results.set_status("interrupted")
        print(f"narwhal: deploy run: {error}", file=sys.stderr)
        return 130
    _publish(deployment)
    ran = [decision.key for decision in result.decisions if decision.ran]
    data: dict[str, Any] = {"run": str(directory), "run_id": result.run_id, "ran": ran}
    if _until(args) == stage_run.TRIAL_STAGE and (found := _trial_result(directory)):
        data["trial"] = found
    results.set_data({**data, **stale(result.stale_lock)})
    print(f"Run {result.run_id} reached {_until(args)}; {len(ran)} stage instances ran.")
    return 0


def _stage_rows(
    record: Mapping[str, Any], directory: Path, target: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Print each stage of the run record; return every row and those of stopped stages."""
    rows = []
    stopped = []
    for name, entry in record["stages"].items():
        row = {
            "stage": name,
            "status": entry["status"],
            "attempt": entry["attempt"],
            "evidence": str(directory / entry["directory"]),
        }
        line = f"{name}: {entry['status']}, attempt {entry['attempt']}, {row['evidence']}"
        if entry["failure"]:
            row["failure"] = entry["failure"]["message"]
            row["recovery"] = target.recovery_url(entry["failure"]["recovery"])
            line += f"\n  {row['failure']}\n  Recovery: {row['recovery']}"
        if entry["status"] in (run_record.FAILED, run_record.TIMEOUT):
            stopped.append(row)
        print(line)
        rows.append(row)
    return rows, stopped


def _live_rows(checks: Any) -> list[dict[str, Any]]:
    for check in checks:
        print(f"{check.key}: {check.state}{': ' + check.detail if check.detail else ''}")
    return [vars(check) for check in checks]


def _stopped_stages(stopped: list[dict[str, Any]]) -> None:
    """Record each stage the run stopped at, which left the fleet incomplete."""
    for row in stopped:
        results.record_error(
            "failed_gate",
            ": ".join(filter(None, (f"{row['stage']} {row['status']}", row.get("failure")))),
            stage=row["stage"],
            context={"recovery": row["recovery"]} if "recovery" in row else None,
        )
    results.set_status("failed_gate")


def _degraded(checks: Any, *, unobserved: bool = False) -> bool:
    """Whether a recorded process or route no longer holds; with `unobserved`, a route the
    workstation could not observe is not counted."""
    fine = ("holds", "restarted and checked", *(("not observed",) if unobserved else ()))
    return any(check.state not in fine for check in checks)


def _status(args: argparse.Namespace, env: dict[str, str]) -> int:
    directory = args.run.resolve()
    locked = False
    target = TARGETS[args.target]
    try:
        record, checks = target.Deployment(directory, env).runner().inspect(refresh=True)
    except RunLocked as error:
        # Another runner holds the run: read it, and check each process without its
        # functional check, recording nothing.
        locked = True
        print(f"{error}; reporting without recording restarts", file=sys.stderr)
        deployment = target.Deployment(directory, env, dry=True)
        record, checks = deployment.runner().inspect(refresh=False)
    rows, stopped = _stage_rows(record, directory, target)
    live = _live_rows(checks)
    results.set_data(
        {
            "run": str(directory),
            "run_id": record["run_id"],
            "locked": locked,
            "stages": rows,
            "live": live,
        }
    )
    # A run that stopped at a failed stage left the fleet incomplete, whatever still holds.
    if stopped:
        _stopped_stages(stopped)
        return 1
    if _degraded(checks):
        results.set_status("degraded")
        return 3
    return 0


def _down(args: argparse.Namespace, env: dict[str, str]) -> int:
    directory = args.run.resolve()
    stopped = TARGETS[args.target].Deployment(directory, env).down()
    for process in stopped:
        state = "stopped" if process["stopped"] else "not running"
        print(f"{process['name']} on {process['host']}: {state}")
    results.set_data({"run": str(directory), "processes": stopped})
    return 0


# `plan`, `run`, `status` and `down` on a Kubernetes cluster: the fleet controller runs the
# stage graph, and the workstation applies the NarwhalFleet, follows it and copies evidence.


def _cluster_fleet(args: argparse.Namespace, env: dict[str, str]) -> Any:
    """The fleet the workstation's inputs describe, or None after reporting each problem."""
    try:
        built = kubernetes_cli.build(env, until=args.until)
    except stage_run.InputError as error:
        for problem in error.problems:
            results.record_error("invalid_input", problem, stage="inputs")
            print(f"narwhal: deploy: {problem}", file=sys.stderr)
        results.set_status("invalid_input")
        return None
    if built.dropped:
        print(
            f"Note: a {fleet_resource.KIND} holds no {', '.join(built.dropped)}; the controller "
            "runs without them"
        )
    return built


def _refused(action: str, error: kubernetes_target.ClusterError) -> int | None:
    """Report an apply the API server refused as invalid; leave any other error to `run`."""
    if error.code != 422:
        return None
    results.record_error("invalid_input", error.message, stage="apply")
    results.set_status("invalid_input")
    print(
        f"narwhal: deploy {action}: the API server refused the fleet: {error.message}",
        file=sys.stderr,
    )
    return 2


def _conflict(action: str, error: kubernetes_cli.FleetConflict, status: str, code: str) -> None:
    fields = [{"manager": manager, "field": path} for manager, path in error.fields]
    results.record_error(code, str(error), stage="apply", context={"fields": fields})
    results.set_status(status)
    print(f"narwhal: deploy {action}: {error}", file=sys.stderr)


def _fleet_line(fleet: Mapping[str, Any]) -> None:
    """Print the fleet's identity and its kstatus conditions."""
    found = kubernetes_cli.summary(fleet)
    states = ", ".join(f"{c['type']} {c['status']}" for c in found["conditions"])
    print(
        f"{fleet_resource.KIND} {found['name']} in {found['namespace']}: "
        f"{states or 'no status yet'}; stage {found['stage'] or 'none'}, engines "
        f"{found['engines'] or '0/0'}, {'serving' if found['serving'] else 'not serving'}"
    )
    stall = kubernetes_cli.conditions(fleet).get(fleet_resource.STALLED) or {}
    if stall.get("status") == "True":
        print(f"  Stalled: {stall.get('reason')}: {stall.get('message')}")


def _evidence(session: kubernetes_cli.Session, action: str) -> dict[str, Any]:
    """Copy the controller's evidence into the run directory; a failed copy is a warning."""
    try:
        return session.export().data()
    except (kubernetes_cli.ExportError, kubernetes_target.ClusterError, OSError) as error:
        print(f"narwhal: deploy {action}: warning: evidence not copied: {error}", file=sys.stderr)
        return {"error": str(error)}


def _cluster_plan(args: argparse.Namespace, env: dict[str, str]) -> int:
    built = _cluster_fleet(args, env)
    if built is None:
        return 2
    directory = args.run.resolve()
    if directory.exists():
        # Task 18's checks hold before plan logs into the run directory.
        check_directory(directory)
    logs = directory if directory.is_dir() else _access_logs()
    session = kubernetes_cli.Session(directory, env, logs=logs / kubernetes_cli.KUBECTL_LOGS)
    if directory.is_dir():
        session.check_target()
    live = session.live()
    if directory.is_dir():
        session.check_bound(live)
    managers = kubernetes_cli.spec_managers(live)
    notes: list[str] = []
    conflict: kubernetes_cli.FleetConflict | None = None
    fleet = built.fleet
    if managers:
        notes.append(
            f"{', '.join(managers)} own the fleet's spec, so run follows the fleet without "
            "applying the workstation's inputs"
        )
        fleet = live
    else:
        try:
            session.apply(built.fleet, dry_run=True)
        except kubernetes_cli.FleetConflict as error:
            conflict = error
        except kubernetes_target.ClusterError as error:
            if (refused := _refused("plan", error)) is not None:
                return refused
            raise
    decisions, more = session.plan(fleet, live, until=_until(args))
    rows = _decisions(decisions)
    notes += more
    for note in notes:
        print(f"Note: {note}")
    results.set_data(
        {
            "run": str(directory),
            "stages": rows,
            "hosts": {},
            "notes": notes,
            "fleet": kubernetes_cli.summary(live) if live else None,
            "managers": managers,
        }
    )
    if conflict is not None:
        _conflict("plan", conflict, "error", "apply_conflict")
        return 4
    return 0


def _cluster_run(args: argparse.Namespace, env: dict[str, str]) -> int:
    built = _cluster_fleet(args, env)
    if built is None:
        return 2
    directory = args.run.resolve()
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    check_directory(directory)
    results.add_artifact("run_record", directory / run_record.RECORD)
    session = kubernetes_cli.Session(directory, env)
    session.check_target()
    timeout = args.timeout or kubernetes_cli.FOLLOW_SECONDS
    with kubernetes_cli.locked(directory):
        live = session.live()
        session.check_bound(live)
        managers = kubernetes_cli.spec_managers(live)
        stale = None
        if managers:
            print(
                f"Note: {', '.join(managers)} own the fleet's spec; following the fleet without "
                "applying the workstation's inputs"
            )
        else:
            before = live
            try:
                live = session.apply(built.fleet)
            except kubernetes_cli.FleetConflict as error:
                _conflict("run", error, "error", "apply_conflict")
                return 4
            except kubernetes_target.ClusterError as error:
                if (refused := _refused("run", error)) is not None:
                    return refused
                raise
            unchanged = before is not None and (
                before["metadata"].get("generation") == live["metadata"].get("generation")
            )
            if unchanged:
                stale = session.retry(live)
        assert live is not None
        session.bind(live)
        uid = live["metadata"]["uid"]
        data: dict[str, Any] = {"run": str(directory), "applied": not managers}
        data["managers"] = managers
        try:
            followed = session.follow(uid=uid, stopped=False, timeout_s=timeout, stale_stall=stale)
        except KeyboardInterrupt:
            results.set_data({**data, "fleet": kubernetes_cli.summary(live)})
            results.record_error("interrupted", "stopped following the fleet", stage="follow")
            results.set_status("interrupted")
            print(
                "narwhal: deploy run: stopped following; the controller keeps running the fleet",
                file=sys.stderr,
            )
            return 130
        data["evidence"] = _evidence(session, "run")
    fleet = followed.fleet or live
    data["fleet"] = kubernetes_cli.summary(fleet)
    if (directory / run_record.RECORD).is_file():
        data["run_id"] = run_record.read(directory)["run_id"]
    return _followed(followed, data, directory, timeout)


def _followed(
    followed: kubernetes_cli.Followed, data: dict[str, Any], directory: Path, timeout: float
) -> int:
    """The result of `run` once following the fleet ended."""
    fleet = data["fleet"]
    if followed.outcome == "ready":
        results.set_data(data)
        print(
            f"The fleet is ready: every stage up to {fleet['stage']} passed, engines "
            f"{fleet['engines']}."
        )
        return 0
    if followed.outcome == "stalled":
        stall = followed.stall or {}
        failed = kubernetes_cli.failure_of(directory)
        status, code, exit_code = kubernetes_cli.stall_result(stall, failed)
        if stall.get("reason") == kubernetes_cli.STAGE_FAILED and failed is not None:
            name, entry = failed
            failure = entry["failure"]
            recovery = kubernetes_target.recovery_url(failure["recovery"])
            data["failed_stage"] = name
            results.record_error(
                code,
                failure["message"],
                stage=name,
                context={**failure["context"], "recovery": recovery, "status": entry["status"]},
            )
            print(
                f"narwhal: deploy run: {name} {entry['status']}: {failure['message']}",
                file=sys.stderr,
            )
            print(f"Recovery: {recovery}", file=sys.stderr)
            print(f"Evidence: {directory / entry['directory']}", file=sys.stderr)
        else:
            message = f"the fleet is stalled: {stall.get('reason')}: {stall.get('message')}"
            data["failed_stage"] = fleet["stage"]
            results.record_error(code, message, stage=fleet["stage"] or "fleet")
            print(f"narwhal: deploy run: {message}", file=sys.stderr)
        results.set_data(data)
        results.set_status(status)
        return exit_code
    if followed.outcome == "gone":
        message = "the fleet was deleted, or made again, while the run followed it"
    else:
        message = (
            f"the fleet was neither ready nor stalled after {timeout:g} s; the controller keeps "
            "running it, and narwhal deploy status reports it"
        )
    results.set_data(data)
    code = "follow_timeout" if followed.outcome == "timeout" else "operation_failed"
    results.record_error(code, message, stage=fleet["stage"] or "fleet")
    results.set_status("error")
    print(f"narwhal: deploy run: {message}", file=sys.stderr)
    return 4


def _bound_session(args: argparse.Namespace, env: dict[str, str]) -> kubernetes_cli.Session:
    """The session of a run directory that follows a fleet, after task 18's checks."""
    directory = args.run.resolve()
    session = kubernetes_cli.Session(directory, env)
    check_directory(directory)
    if session.binding() is None:
        raise ValueError(
            f"{directory} follows no {fleet_resource.KIND}; run narwhal deploy run --target "
            "kubernetes first"
        )
    return session


def _cluster_status(args: argparse.Namespace, env: dict[str, str]) -> int:
    session = _bound_session(args, env)
    directory = session.directory
    live = session.live()
    session.check_bound(live)
    assert live is not None
    locked = False
    evidence: dict[str, Any] = {}
    try:
        with kubernetes_cli.locked(directory):
            evidence = _evidence(session, "status")
    except RunLocked as error:
        locked = True
        print(f"{error}; reporting the run record as last copied", file=sys.stderr)
    _fleet_line(live)
    rows: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    checks: Any = ()
    run_id = None
    if (directory / run_record.RECORD).is_file():
        record, checks = session.deployment(live).runner().inspect(refresh=False)
        run_id = record["run_id"]
        rows, stopped = _stage_rows(record, directory, kubernetes_target)
    live_rows = _live_rows(checks)
    results.set_data(
        {
            "run": str(directory),
            "run_id": run_id,
            "locked": locked,
            "stages": rows,
            "live": live_rows,
            "fleet": kubernetes_cli.summary(live),
            "evidence": evidence,
        }
    )
    if stopped:
        _stopped_stages(stopped)
        return 1
    stall = kubernetes_cli.conditions(live).get(fleet_resource.STALLED) or {}
    if stall.get("status") == "True":
        # The same result `run` gives the stall: a refused input is invalid input.
        status, code, exit_code = kubernetes_cli.stall_result(
            stall, kubernetes_cli.failure_of(directory)
        )
        results.record_error(
            code,
            f"the fleet is stalled: {stall.get('reason')}: {stall.get('message')}",
            stage="fleet",
        )
        results.set_status(status)
        return exit_code
    # Observing a route takes a Job on each node; the controller observes it on every pass.
    if _degraded(checks, unobserved=True):
        results.set_status("degraded")
        return 3
    return 0


def _recorded_processes(session: kubernetes_cli.Session) -> list[dict[str, Any]]:
    """Each process the mirrored record started, and whether its pod runs it until now."""
    directory = session.directory
    if not (directory / run_record.RECORD).is_file():
        return []
    found = []
    for entry in run_record.read(directory)["stages"].values():
        if entry["stage"] not in ("router-start", "sidecar-start", "engine-start"):
            continue
        if entry["status"] != run_record.PASSED:
            continue
        for process in entry["processes"]:
            pod = session.ws.get(kubernetes_target.POD, process.get("pod", process["name"]))
            recorded = process.get("pod_uid")
            ran = pod is not None and recorded in (None, pod["metadata"].get("uid"))
            found.append(
                {"name": process["name"], "host": process.get("host", "cluster"), "stopped": ran}
            )
    return found


def _cluster_down(args: argparse.Namespace, env: dict[str, str]) -> int:
    session = _bound_session(args, env)
    directory = session.directory
    timeout = args.timeout or kubernetes_cli.STOP_SECONDS
    data: dict[str, Any] = {"run": str(directory), "stopped": False, "deleted": False}
    with kubernetes_cli.locked(directory):
        live = session.live()
        if live is None:
            print(
                f"{env.get(kubernetes_target.NAMESPACE)} holds no {fleet_resource.KIND}; "
                "nothing to stop"
            )
            results.set_data({**data, "fleet": None, "processes": []})
            return 0
        session.check_bound(live)
        managers = kubernetes_cli.spec_managers(live)
        if managers:
            raise ValueError(
                f"down changed nothing: {', '.join(managers)} own the fleet's spec; set "
                "spec.stopped through them"
            )
        processes = _recorded_processes(session)
        uid = live["metadata"]["uid"]
        try:
            session.stop(live)
        except kubernetes_cli.FleetConflict as error:
            _conflict("down", error, "error", "apply_conflict")
            return 4
        followed = session.follow(uid=uid, stopped=True, timeout_s=timeout)
        data["stopped"] = followed.outcome == "stopped"
        if followed.outcome != "gone":
            data["evidence"] = _evidence(session, "down")
        # A fleet that has not stopped is never deleted: its evidence is not all copied yet.
        if args.delete and data["stopped"]:
            data["deleted"] = session.delete(uid)
    for process in processes:
        state = "stopped" if process["stopped"] else "not running"
        print(f"{process['name']} on {process['host']}: {state}")
    data["fleet"] = kubernetes_cli.summary(followed.fleet or live)
    data["processes"] = processes
    results.set_data(data)
    if data["deleted"]:
        print(f"Deleted the {fleet_resource.KIND}; the garbage collector removes what it owned.")
    elif data["stopped"]:
        print("The fleet is stopped; its volumes stay. narwhal deploy run starts it again.")
    if not data["stopped"] and not data["deleted"]:
        message = (
            f"the controller did not report the fleet stopped within {timeout:g} s"
            + ("; down did not delete it" if args.delete else "")
            if followed.outcome == "timeout"
            else "the fleet was deleted while down waited for it to stop"
        )
        code = "follow_timeout" if followed.outcome == "timeout" else "operation_failed"
        results.record_error(code, message, stage="down")
        results.set_status("error")
        print(f"narwhal: deploy down: {message}", file=sys.stderr)
        return 4
    return 0


CLUSTER_COMMANDS: dict[str, Any] = {
    "plan": _cluster_plan,
    "run": _cluster_run,
    "status": _cluster_status,
    "down": _cluster_down,
}


# `plan`, `run`, `status` and `down` on SSH hosts: the stage graph runs in this process.
STAGE_COMMANDS: dict[str, Any] = {"plan": _plan, "run": _run, "status": _status, "down": _down}


def _check_options(args: argparse.Namespace) -> None:
    """Refuse the options of another target."""
    if args.action in ("plan", "run"):
        _check_trial(args)
    if args.target != "kubernetes":
        for option in ("timeout", "delete"):
            if getattr(args, option, None):
                raise ValueError(
                    f"--{option} follows a Kubernetes cluster's {fleet_resource.KIND}; use it "
                    "with --target kubernetes"
                )


def _dispatch(args: argparse.Namespace, env: dict[str, str]) -> int:
    if args.action in STAGE_COMMANDS:
        _check_options(args)
        commands = CLUSTER_COMMANDS if args.target == "kubernetes" else STAGE_COMMANDS
        try:
            return commands[args.action](args, env)
        except (RunLocked, HelpersSurvived) as error:
            # Another runner holds the run, or this run's helpers outlived it.
            found = getattr(error, "stale_lock", None)
            if found is not None:
                print(f"narwhal: deploy {args.action}: {stale_notice(found)}", file=sys.stderr)
                results.set_data({"stale_lock": dict(found)})
            results.record_error("operation_failed", str(error), stage=args.action)
            results.set_status("error")
            print(f"narwhal: deploy {args.action}: {error}", file=sys.stderr)
            return 4
    if args.action in ("shell", "tunnel") and results.json_mode():
        raise ValueError(f"deploy {args.action} is interactive; run it without --format json")
    if args.action == "discover":
        return _discover(args, env)
    if args.action == "cache-groups":
        return _cache_groups(args, env)
    hosts = _hosts(args, env)
    if args.action == "access":
        return _access(hosts, env)
    actions = {
        "package": _package,
        "install": _install,
        "transfer-measure": _transfer_measure,
        "fabric": _fabric,
        "shell": _shell,
        "tunnel": _tunnel,
    }
    return actions[args.action](args, hosts, env)


def run(args: argparse.Namespace) -> int:
    """Run one `narwhal deploy` action and return its exit code."""
    results.set_operation("deploy " + args.action)
    context = f"deploy {args.action}"
    try:
        return _dispatch(args, dict(os.environ))
    except (
        RemoteError,
        RemoteTimeout,
        kubernetes_target.ClusterError,
        kubernetes_cli.FleetConflict,
        fabric.FabricFailure,
        remote.StepFailed,
        supervision.SupervisionError,
    ) as exc:
        if results.json_mode():
            raise
        return failure("narwhal", context, exc, 1)
    except (ValueError, KeyError, TypeError, FileExistsError, FileNotFoundError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal", context, exc, 2)
    except OSError as exc:
        if results.json_mode():
            raise
        return failure("narwhal", context, exc, 1)
