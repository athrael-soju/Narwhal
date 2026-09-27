"""Execute registered dev lifecycle plans through the installed finite commands."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from narwhal.deployment import native_engine, stages
from narwhal.deployment.management_access import (
    AccessError,
    directory,
    read_input,
    read_object,
)
from narwhal.deployment.management_adapters import AdapterManifest, PreparedPlan
from narwhal.deployment.management_commands import CommandError, _validate_output
from narwhal.deployment.management_context import command_context
from narwhal.deployment.management_executor import ReconcileOutcome, StageContext, StageOutcome
from narwhal.deployment.management_exports import public_value
from narwhal.deployment.management_records import OperationError, utc_now, uuid_string
from narwhal.deployment.management_registry import ManagementTarget
from narwhal.diagnostics.management_artifacts import ArtifactError, ArtifactStore

_KINDS = {"router": "router"}
_FILES = ("instance.json", "template.json", "fleet.json", "engine-launch.json")


def _owner(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperationError("recovery_required", "Dev ownership is unavailable")
    try:
        operation_id = uuid_string(value["operation_id"])
        stage_id = value["stage_id"]
        launch_token = value["launch_token"]
        if launch_token is not None:
            launch_token = uuid_string(launch_token)
        if not isinstance(stage_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", stage_id
        ):
            raise ValueError("stage")
    except (KeyError, TypeError, ValueError):
        raise OperationError("recovery_required", "Dev ownership is invalid") from None
    return {"operation_id": operation_id, "stage_id": stage_id, "launch_token": launch_token}


def _root(context: StageContext) -> Path:
    root = context.target.instance_dir
    if root is None:
        raise OperationError("invalid_input", "Dev target has no instance directory")
    return root


def _state(root: Path) -> tuple[dict[str, Any], Path]:
    with directory(root, private=True):
        state = read_object(root / "lifecycle.json")
    run = Path(state.get("run", ""))
    if run.parent != root or not re.fullmatch(r"run-[0-9a-f]{12}", run.name):
        raise OperationError("permission_denied", "Dev generation path is outside its instance")
    with directory(run, private=True):
        pass
    if not isinstance(state.get("processes"), list) or len(state["processes"]) > 17:
        raise OperationError("invalid_input", "Dev process ownership is invalid")
    return state, run


def _resource(root: Path, owner: dict[str, Any], name: str) -> str:
    key = hashlib.sha256(str(root).encode()).hexdigest()
    return f"dev:{key}:{owner['launch_token'] or 'existing'}:{name}"


def _receipt(
    root: Path,
    owner: dict[str, Any],
    name: str,
    *,
    run: Path | None = None,
    identity: dict[str, Any] | None = None,
    effect: str = "unknown",
) -> dict[str, Any]:
    return {
        "resource_id": _resource(root, owner, name),
        "kind": _KINDS.get(name, "engine" if name.startswith("engine-") else "attestation"),
        "host_id": "management",
        "owner": owner,
        "identity": {
            **identity,
            "instance_dir": str(root),
            "run": str(run),
            "name": name,
        }
        if identity is not None
        else None,
        "effect": effect,
        "observed_at": utc_now(),
    }


def _process(identity: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Inspect a recorded group and its descendants without signalling anything."""
    try:
        boot = uuid_string(identity["boot_id"])
        pid, ticks = identity["pid"], identity["start_ticks"]
        if type(pid) is not int or pid <= 1 or type(ticks) is not int or ticks <= 0:
            raise ValueError("identity")
    except (KeyError, TypeError, ValueError):
        raise OperationError("recovery_required", "Dev process identity is invalid") from None
    basic = {"boot_id": boot, "pid": pid, "start_ticks": ticks}
    owned = {pid: ticks}
    if native_engine._owns_process(basic):
        members = stages._discover(owned, pid)
        processes = {
            str(number): row[3] for number, row in members.items() if row[0] not in {"Z", "X"}
        }
        return "confirmed", {**basic, "processes": processes}
    if native_engine._group_members(basic):
        # A missing group leader cannot authorize adopting otherwise unrecorded PIDs.
        return "unknown", {**basic, "processes": {str(pid): ticks}}
    return "absent", {**basic, "processes": {str(pid): ticks}}


def _names(root: Path) -> list[str]:
    config = read_object(root / "instance.json")
    count = config.get("engine_count")
    if type(count) is not int or not 2 <= count <= 8:
        raise OperationError("invalid_input", "Dev engine allocation is invalid")
    return [
        *[f"engine-{index}" for index in range(1, count + 1)],
        *[f"sidecar-{index}" for index in range(1, count + 1)],
        "router",
    ]


def _services(
    context: StageContext,
    owner: dict[str, Any],
    *,
    starting: bool = False,
    settled: bool = False,
    require_live: bool = False,
) -> list[dict[str, Any]]:
    root = _root(context)
    names = _names(root)
    try:
        state, run = _state(root)
    except AccessError as error:
        if error.code != "input_missing":
            raise
        return (
            [
                _receipt(root, owner, name, effect="absent" if settled else "unknown")
                for name in names
            ]
            if starting
            else []
        )
    if starting and state.get("management_owner") != owner:
        return [
            _receipt(root, owner, name, effect="absent" if settled else "unknown") for name in names
        ]
    if require_live and state.get("phase") not in {"launched", "ready"}:
        raise OperationError("recovery_required", "Dev startup did not complete its generation")
    records: dict[str, dict[str, Any]] = {}
    for record in state["processes"]:
        if not isinstance(record, dict) or record.get("name") not in names:
            raise OperationError("recovery_required", "Dev service ownership is invalid")
        if record["name"] in records:
            raise OperationError("recovery_required", "Dev service ownership is duplicated")
        records[record["name"]] = record
    receipts = []
    for name in names:
        selected_owner = owner
        if name.startswith("engine-"):
            try:
                identity = read_object(run / name / "native-process.json")
            except AccessError as error:
                if error.code != "input_missing":
                    raise
                identity = None
            if identity is not None:
                try:
                    selected_owner = _owner(read_object(run / name / "management-owner.json"))
                except AccessError as error:
                    if error.code != "input_missing" or starting:
                        raise
        else:
            record = records.get(name)
            identity = record.get("identity") if record else None
            if record and record.get("management_owner") is not None:
                selected_owner = _owner(record["management_owner"])
        if starting and identity is not None and selected_owner != owner:
            raise OperationError("recovery_required", "Dev service belongs to another launch")
        if identity is None:
            if require_live:
                raise OperationError(
                    "recovery_required", "Dev startup has incomplete process receipts"
                )
            if starting:
                receipts.append(
                    _receipt(root, owner, name, run=run, effect="absent" if settled else "unknown")
                )
            continue
        effect, observed = _process(identity)
        if require_live and effect != "confirmed":
            raise OperationError("recovery_required", "Dev startup lost an owned service")
        receipts.append(
            _receipt(root, selected_owner, name, run=run, identity=observed, effect=effect)
        )
    return receipts


def _instance_receipt(
    context: StageContext, owner: dict[str, Any], *, settled: bool
) -> dict[str, Any]:
    root = _root(context)
    hashes = {}
    missing = 0
    for name in _FILES:
        try:
            hashes[name] = hashlib.sha256(read_input(root / name)).hexdigest()
        except AccessError as error:
            if error.code != "input_missing":
                raise
            missing += 1
    effect = (
        "confirmed"
        if len(hashes) == len(_FILES)
        else "absent"
        if missing == len(_FILES) and settled
        else "unknown"
    )
    return {
        "resource_id": _resource(root, owner, "instance"),
        "kind": "private_artifact",
        "host_id": "management",
        "owner": owner,
        "identity": {"instance_dir": str(root), "files": hashes} if hashes else None,
        "effect": effect,
        "observed_at": utc_now(),
    }


def _helpers_stopped(operation: dict[str, Any]) -> bool:
    helpers = [
        item
        for stage in operation["stages"]
        for item in stage["effects"]
        if item["kind"] == "measurement_helper"
    ]
    return bool(helpers) and all(
        item["identity"] is not None
        and not stages.active(item["identity"])
        and item["effect"] == "absent"
        for item in helpers
    )


class LocalDevAdapter:
    """Bind the existing native dev CLI to durable management execution."""

    @property
    def manifest(self) -> AdapterManifest:
        """Verify the installed adapter and source identity before admission."""
        from .management_prepare import manifest

        return manifest()

    def prepare(
        self,
        context: StageContext,
        target: ManagementTarget,
        action: str,
        parameters: dict[str, Any],
    ) -> PreparedPlan:
        """Capture the registered recipe, physical resources and current instance."""
        from .management_prepare import snapshot

        return snapshot(context, target, action, parameters)

    def check(self, context: StageContext, target: ManagementTarget, plan: dict[str, Any]) -> None:
        """Recheck prerequisites and immutable inputs before the lifecycle command."""
        from .management_prepare import check

        check(context, target, plan)

    def execute_stage(
        self, context: StageContext, stage: dict[str, Any], plan: dict[str, Any]
    ) -> StageOutcome:
        """Run exactly one installed dev action and preserve its command result."""
        from .management_prepare import execution_config

        action = plan["payload"]["action"]
        if action not in {"dev_init", "dev_up", "dev_verify", "dev_down"} or stage[
            "operation"
        ] != action.replace("_", ".", 1):
            raise OperationError("unsupported_action", "Dev stage operation is unsupported")
        config = execution_config(context, plan)
        arguments = list(config["arguments"])
        if config["template"] is not None:
            with directory(context.target.artifact_root, private=True, create=True):
                pass
            with directory(context.output_dir, private=True, create=True) as parent:
                descriptor = os.open(
                    "dev-template.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(config["template"])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(parent)
            arguments.extend(["--template", str(context.output_dir / "dev-template.json")])
        command = [sys.executable, "-m", "narwhal.dev.cli", *arguments, "--format", "json"]
        environment = context.access.environment(context.target, config["fleet_document"])
        for index, value in enumerate(sorted(context.redactor.secrets)):
            environment[f"NARWHAL_MCP_SECRET_{index}"] = value
        with command_context(
            context, command=command, input_hashes=config["input_hashes"]
        ) as credential:
            owner = _owner(credential.owner)
            if action == "dev_init":
                intent = _instance_receipt(context, owner, settled=False)
                intent["effect"] = "unknown"
                context.record_intent(intent)
            elif action == "dev_up":
                for name in _names(_root(context)):
                    context.record_intent(_receipt(_root(context), owner, name))
            elif action == "dev_down":
                for receipt in _services(context, owner):
                    receipt["effect"] = "unknown"
                    context.record_intent(receipt)

            def retain(evidence: dict[str, Any]) -> list[dict[str, Any]]:
                context.assert_current()
                return _services(context, owner, starting=True, require_live=True)

            try:
                result = context.run_command(
                    command,
                    cwd=context.target.working_directory,
                    env={**environment, **credential.env},
                    pass_fds=credential.pass_fds,
                    retain_on_success=retain if action == "dev_up" else None,
                )
            except BaseException:
                self._record_after(context, owner, action, settled=_helpers_stopped(context.read()))
                raise
            self._record_after(context, owner, action, settled=True)
        try:
            document = _validate_output(result.stdout.encode(), result.returncode)
        except CommandError as error:
            raise OperationError(error.code, error.message) from None
        if document["operation"] != "dev " + action.removeprefix("dev_"):
            raise OperationError("invalid_input", "Dev command returned another operation")
        artifacts = self._exports(context, document)
        return StageOutcome(
            status=document["status"],
            data=document["data"],
            command_result=document,
            artifacts=artifacts,
            errors=document["errors"],
        )

    def _record_after(
        self, context: StageContext, owner: dict[str, Any], action: str, *, settled: bool
    ) -> None:
        if action == "dev_init":
            context.record_effect(_instance_receipt(context, owner, settled=settled))
        elif action in {"dev_up", "dev_down"}:
            for receipt in _services(context, owner, starting=action == "dev_up", settled=settled):
                context.record_effect(receipt)

    def _exports(self, context: StageContext, document: dict[str, Any]) -> list[dict[str, Any]]:
        store = ArtifactStore(str(context.registry.registry_id), context.target)
        result = [
            store.export(
                json.dumps(public_value(context.access, context.target, document)).encode(),
                "command_result",
            )
        ]
        root = _root(context)
        # Read only the fixed documents owned by this action, never arbitrary
        # paths returned in the CLI result's artifact list.
        for name in (*_FILES, "lifecycle.json"):
            try:
                value = read_object(root / name)
                result.append(
                    store.export(
                        json.dumps(public_value(context.access, context.target, value)).encode(),
                        "dev_" + name.removesuffix(".json"),
                    )
                )
            except AccessError as error:
                if error.code != "input_missing":
                    raise
        return result

    def reconcile(self, context: StageContext, operation: dict[str, Any]) -> ReconcileOutcome:
        """Inspect saved instances and process identities without launching or stopping."""
        effects = []
        errors = []
        helpers_stopped = True
        settled = _helpers_stopped(operation)
        for row in operation["stages"]:
            if row["state"] in {"succeeded", "reused"}:
                continue
            for receipt in row["effects"]:
                observed = dict(receipt)
                identity = receipt.get("identity")
                try:
                    if receipt["kind"] == "measurement_helper":
                        if identity is not None and not stages.active(identity):
                            observed.update(effect="absent", observed_at=utc_now())
                        elif receipt["effect"] != "absent":
                            helpers_stopped = False
                    elif receipt["kind"] in {"engine", "attestation", "router"}:
                        if identity is not None:
                            effect, current = _process(identity)
                            observed.update(
                                effect=effect,
                                identity={**identity, **current},
                                observed_at=utc_now(),
                            )
                        else:
                            candidates = _services(
                                context, _owner(receipt["owner"]), starting=True, settled=settled
                            )
                            observed = next(
                                (
                                    item
                                    for item in candidates
                                    if item["resource_id"] == receipt["resource_id"]
                                ),
                                observed,
                            )
                    elif receipt["kind"] == "private_artifact" and receipt["resource_id"].endswith(
                        ":instance"
                    ):
                        observed = _instance_receipt(
                            context, _owner(receipt["owner"]), settled=settled
                        )
                except (AccessError, OperationError, OSError, ArtifactError):
                    observed["effect"] = "unknown"
                    errors.append(
                        {
                            "code": "recovery_required",
                            "message": "Dev ownership could not be reconciled",
                        }
                    )
                effects.append(observed)
        return ReconcileOutcome(
            helpers_stopped=helpers_stopped,
            complete=operation["tool"] != "plan_prepare"
            and all(row["state"] in {"succeeded", "reused"} for row in operation["stages"]),
            effects=effects,
            errors=errors,
        )
