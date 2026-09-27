"""Execute the recorded fleet gates through bounded, owned SSH jobs."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from narwhal.diagnostics.management_artifacts import ArtifactStore

from . import ssh_prepare, ssh_settings
from .management_access import directory, read_input
from .management_adapters import AdapterManifest, PreparedPlan
from .management_executor import ReconcileOutcome, StageContext, StageOutcome
from .management_exports import public_value
from .management_records import OperationError, encode_record, utc_now
from .management_registry import ManagementTarget
from .ssh_files import SSHFiles
from .ssh_transport import SSHTransport

_FINISHED = {"succeeded", "failed", "cancelled", "timed_out", "recovery_required"}


def remote_absent(receipt: dict[str, Any]) -> bool:
    """Require completed cleanup observations before declaring remote effects absent."""
    return (
        receipt.get("state") in _FINISHED - {"recovery_required"}
        and not receipt.get("observed_processes")
        and not receipt.get("containers")
        and not receipt.get("container_error")
        and not receipt.get("cleanup", {}).get("error")
    )


def _effect(context: StageContext, host: str, job_id: str, kind: str) -> dict[str, Any]:
    return {
        "resource_id": f"ssh:{host}:{job_id}",
        "host_id": host,
        "kind": kind,
        "owner": {
            "operation_id": context.operation_id,
            "stage_id": context.stage_id,
            "launch_token": job_id,
        },
        "identity": None,
        "effect": "unknown",
        "observed_at": utc_now(),
    }


class Session:
    """Carry verified inputs and record each remote intention before dispatch."""

    def __init__(self, context: StageContext, plan: dict[str, Any]):
        self.context, self.plan = context, plan
        self.artifacts: list[dict[str, Any]] = []
        self.execution = ssh_prepare.input_document(context, plan, "runtime_identity")["execution"]
        self.settings = ssh_settings.SSHSettings.model_validate_json(
            encode_record(self.execution["settings"])
        )
        self.recipe = ssh_settings.SSHRecipe.model_validate_json(
            encode_record(self.execution["recipe"])
        )
        self.fleet = ssh_prepare.input_document(context, plan, "fleet_config")
        self.environment, self.hosts = ssh_prepare._host_environment(
            context, context.target, self.settings, self.recipe, self.fleet
        )
        self.transport = SSHTransport(context, self.settings, self.hosts, self.environment)
        self.files = SSHFiles(self.transport, context, self.settings.remote_root)
        self.relative = "operations/" + context.operation_id
        self.root = Path(self.settings.remote_root) / self.relative
        self.state = ssh_prepare.deployment_state(
            context.target, registry_id=str(context.registry.registry_id)
        )
        if not self.state and plan["payload"]["action"] == "deployment_cleanup":
            self.state = self.execution["prior_state"]
        if not self.state or (
            plan["payload"]["action"] == "fleet_deploy"
            and self.state.get("operation_id") != context.operation_id
        ):
            self.state = {
                "schema": "narwhal.ssh-deployment",
                "schema_version": 1,
                "registry_id": str(context.registry.registry_id),
                "target_id": context.target_id,
                "operation_id": context.operation_id,
                "recipe_id": self.execution["recipe_id"],
                "status": "deploying",
                "checkouts": {},
                "engines": {},
                "services": [],
                "gates": {},
            }
        self.router_host = next(host.id for host in self.hosts if "router" in host.roles)

    def save(self) -> None:
        """Persist the deployment generation separately from immutable gate evidence."""
        self.context.assert_current()
        self.state["last_operation_id"] = self.context.operation_id
        root = self.context.target.artifact_root
        with directory(root, private=True, create=True) as parent:
            name = ".deployment-" + self.context.operation_id + ".json"
            fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encode_record(self.state))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, "deployment-state.json", src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=parent)

    def host_for(self, role: str) -> str:
        """Resolve a registered role to its single host alias."""
        return next(host.id for host in self.hosts if role in host.roles)

    def checkout(self, host: str) -> Path:
        """Return the verified checkout recorded by the installation stage."""
        return Path(self.state["checkouts"][host])

    def role_environment(self, role: str) -> dict[str, str]:
        """Resolve credentials for one role immediately before job submission."""
        values = {"PATH": os.defpath, "LANG": "C.UTF-8", **self.execution["role_environment"][role]}
        for name, reference in self.execution["credential_fields"][role].items():
            value = self.environment.get(reference)
            if not value:
                raise OperationError(
                    "prerequisite_failed", "A registered role credential is unavailable"
                )
            values[name] = value
        return values

    def put(self, host: str, name: str, content: bytes) -> Path:
        """Transfer an immutable input under this operation's remote directory."""
        self.files.put(host, self.relative + "/" + name, content)
        return self.root / name

    def export(self, kind: str, value: Any, *, complete: bool = True) -> dict[str, Any]:
        """Expose fixed structured evidence and retain its reference before advancing."""
        context = self.context
        reference = ArtifactStore(str(context.registry.registry_id), context.target).export(
            encode_record(public_value(context.access, context.target, value)),
            kind,
            complete=complete,
        )

        def retain(record: dict[str, Any]) -> None:
            record["artifacts"].append(reference)
            stage = next(row for row in record["stages"] if row["stage_id"] == context.stage_id)
            stage["artifacts"].append(reference)

        context.update(retain)
        self.artifacts.append(reference)
        return reference

    def command(
        self,
        host: str,
        name: str,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        kind: str = "measurement_helper",
        containers: bool = False,
        background: bool = False,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Start one idempotent remote job after recording its exact owner."""
        context = self.context
        job_id = job_id or str(
            uuid5(UUID(context.operation_id), f"{context.stage_id}:{host}:{name}")
        )
        effect = _effect(context, host, job_id, kind)
        self.export(
            "ssh_command",
            {
                "host_id": host,
                "owner": effect["owner"],
                "argv": argv,
                "cwd": str(cwd),
                "environment_names": sorted(env),
                "containers": containers,
                "background": background,
            },
        )
        context.record_intent(effect)
        cleanup = context.stage["cleanup"]
        timeout_ms = max(1, int((context.deadline - time.monotonic()) * 1000))
        row = self.transport.submit(
            host,
            job_id=job_id,
            operation_id=context.operation_id,
            stage_id=context.stage_id,
            fence=context.fence,
            argv=argv,
            cwd=str(cwd),
            env=env,
            timeout_ms=timeout_ms,
            term_grace_ms=cleanup["term_grace_ms"],
            kill_grace_ms=cleanup["kill_grace_ms"],
            containers=containers,
            secret_env=sorted(
                {
                    field
                    for fields in self.execution["credential_fields"].values()
                    for field in fields
                    if field in env
                }
            ),
        )
        if row.get("owner") != effect["owner"]:
            raise OperationError("ownership_conflict", "Remote admission receipt changed owner")
        effect["identity"] = {"job_id": job_id, "receipt": row}
        effect["effect"] = "confirmed"
        context.record_effect(effect)
        if background:
            return effect
        while True:
            context.assert_current()
            row = self.transport.status(host, job_id)
            if row["owner"] != effect["owner"]:
                raise OperationError(
                    "ownership_conflict", "Remote job receipt belongs to another owner"
                )
            if row["state"] in _FINISHED | {"awaiting_retain"}:
                break
            time.sleep(min(0.2, max(0, context.deadline - time.monotonic())))
        effect["identity"] = {"job_id": job_id, "receipt": row}
        if remote_absent(row):
            effect["effect"] = "absent"
        context.record_effect(effect)
        if row["state"] not in {"succeeded", "awaiting_retain"} or (
            row["state"] == "succeeded" and not remote_absent(row)
        ):
            with contextlib.suppress(OperationError, ValueError, OSError):
                self._failed_output(host, job_id, row)
            raise OperationError("command_failed", "Remote gate job did not complete successfully")
        return effect

    def _failed_output(self, host: str, job_id: str, receipt: dict[str, Any]) -> None:
        """Export bounded failed-job output without replacing the initiating failure."""
        streams: dict[str, Any] = {}
        for stream in ("stdout", "stderr"):
            try:
                result = self.transport.read(host, job_id, stream, max_bytes=65536)
                data = base64.b64decode(result["data_base64"], validate=True)
                if (
                    result["job_id"] != job_id
                    or result["stream"] != stream
                    or result["offset"] != 0
                    or len(data) > 65536
                    or type(result["bytes"]) is not int
                    or result["bytes"] < len(data)
                    or result["next_offset"] != (len(data) if result["bytes"] > len(data) else None)
                ):
                    raise ValueError("Remote failure output has an invalid identity or bound")
                streams[stream] = {
                    "log": data.decode("utf-8", errors="replace"),
                    "bytes": result["bytes"],
                    "captured_bytes": len(data),
                    "truncated": result["next_offset"] is not None
                    or bool(receipt.get(stream + "_truncated")),
                }
            except (OperationError, ValueError, OSError, KeyError, TypeError) as error:
                streams[stream] = {
                    "omitted": True,
                    "error": error.code
                    if isinstance(error, OperationError)
                    else "source_unavailable",
                }
        self.export(
            "ssh_command_output",
            {
                "host_id": host,
                "owner": receipt["owner"],
                "state": receipt["state"],
                "exit_code": receipt.get("exit_code"),
                "streams": streams,
            },
            complete=all(
                not row.get("omitted") and not row.get("truncated") for row in streams.values()
            ),
        )

    def gate(
        self,
        host: str,
        name: str,
        request: dict[str, Any],
        *,
        role: str = "router",
        kind: str = "measurement_helper",
        containers: bool = False,
        background: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Run one fixed installed gate and read its private result."""
        job_id = str(
            uuid5(UUID(self.context.operation_id), f"{self.context.stage_id}:{host}:{name}")
        )
        request_name = f"jobs/{job_id}/request.json"
        result_name = f"jobs/{job_id}/result.json"
        owner = _effect(self.context, host, job_id, kind)["owner"]
        payload = {**request, "owner": owner, "result_path": str(self.root / result_name)}
        path = self.put(host, request_name, encode_record(payload))
        arguments = [
            str(self.checkout(host) / ".venv/bin/python"),
            "-m",
            "narwhal.deployment.ssh_gates",
            "--request",
            str(path),
        ]
        structured_command = request["operation"] in {"profile", "preflight", "postload"}

        def capture_command() -> None:
            raw = self.files.read(
                host,
                self.relative + f"/jobs/{job_id}/command-result.json",
                max_bytes=8 * 1024 * 1024,
            )
            self.export("command_result", json.loads(raw))

        try:
            effect = self.command(
                host,
                name,
                arguments,
                cwd=self.checkout(host),
                env=self.role_environment(role),
                kind=kind,
                containers=containers,
                background=background,
                job_id=job_id,
            )
        except OperationError:
            if structured_command:
                with contextlib.suppress(OperationError, ValueError, OSError):
                    capture_command()
            raise
        if background:
            return effect, None
        if structured_command:
            capture_command()
        result = json.loads(
            self.files.read(host, self.relative + "/" + result_name, max_bytes=1024 * 1024)
        )
        if result["owner"] != owner or result["operation"] != request["operation"]:
            raise OperationError("ownership_conflict", "Remote gate result has a different owner")
        self.export("ssh_gate_result", result)
        documents: list[tuple[str, Path]] = []
        operation = request["operation"]
        if operation == "launch":
            documents = [
                (name, Path(request["run"]) / (name + ".json"))
                for name in ("launch", "checked", "cache-layout")
            ]
        elif operation == "attest":
            documents = [("engine_attestation", Path(result["result"]["document"]))]
        elif operation == "profile":
            profiles = Path(result["result"]["profiles_path"])
            documents = [
                ("profiles", profiles),
                ("profile_samples", profiles.with_suffix(".samples.json")),
            ]
        elif operation == "router_capture":
            documents = [("preserved_handoff", Path(request["preserved_handoff"]))]
        for label, document in documents:
            relative = document.relative_to(self.settings.remote_root).as_posix()
            if not relative.startswith(self.relative + "/"):
                raise OperationError("ownership_conflict", "Gate evidence is outside its operation")
            data = self.files.read(host, relative, max_bytes=8 * 1024 * 1024)
            self.export(label, json.loads(data))
        return effect, result["result"]

    def retain(self, effect: dict[str, Any]) -> None:
        """Transfer one verified service to the recorded deployment generation."""
        host, job_id = effect["host_id"], effect["owner"]["launch_token"]
        row = self.transport.retain(host, job_id)
        while row["state"] != "retained":
            self.context.assert_current()
            if row["state"] in _FINISHED:
                raise OperationError(
                    "recovery_required", "Service exited before retention completed"
                )
            time.sleep(0.2)
            row = self.transport.status(host, job_id)
        if row["owner"] != effect["owner"]:
            raise OperationError("ownership_conflict", "Service receipt changed before retention")
        effect["identity"] = {"job_id": job_id, "receipt": row}
        effect["effect"] = "confirmed"
        self.context.record_effect(effect)
        self.state["services"].append(effect)
        self.save()

    def check_generations(self) -> None:
        """Reject replaced engines and GPU clients outside recorded container trees."""
        if self.plan["payload"]["action"] == "deployment_cleanup":
            return
        owned_processes: dict[str, set[tuple[int, int]]] = {}
        for role, engine in self.state.get("engines", {}).items():
            effect = engine["effect"]
            host = effect["host_id"]
            job_id = effect["owner"]["launch_token"]
            status = self.transport.status(host, job_id)
            if status["owner"] != effect["owner"] or status["state"] != "retained":
                raise OperationError(
                    "recovery_required", "An engine supervisor is absent or changed"
                )
            inspected = self.transport.inspect_containers(host, job_id)["containers"]
            containers = [row for row in inspected if row["container_id"] == engine["container_id"]]
            if len(containers) != 1 or not containers[0]["running"]:
                raise OperationError(
                    "stale_plan", "A recorded serving container is absent or stopped"
                )
            processes = owned_processes.setdefault(host, set())
            process = containers[0].get("process")
            if process is None:
                raise OperationError(
                    "recovery_required", "Serving container process is unavailable"
                )
            processes.add((process["pid"], process["start_ticks"]))
            processes.update(
                (int(pid), ticks) for pid, ticks in containers[0]["descendants"].items()
            )
            role_env = self.role_environment(role)
            parameters = {"url": role_env[f"NARWHAL_NODE_{role.removeprefix('engine-')}_URL"]}
            if role_env.get("NARWHAL_ENGINE_API_KEY"):
                parameters["api_key"] = role_env["NARWHAL_ENGINE_API_KEY"]
            current = self.transport.probe(host, "generation", parameters)
            previous = engine["generation"]
            if current["version"] != previous["vllm_version"] or Decimal(
                current["process_start_time_seconds"]
            ) != Decimal(str(previous["process_start_time_seconds"])):
                raise OperationError(
                    "stale_plan", "Engine generation changed; its qualification evidence is invalid"
                )
        for host, owned in owned_processes.items():
            inventory = self.transport.probe(host, "inventory", {})
            clients = inventory["gpu_clients"]
            if not clients["complete"]:
                raise OperationError("prerequisite_failed", "GPU client inventory is incomplete")
            if any((row["pid"], row["start_ticks"]) not in owned for row in clients["processes"]):
                raise OperationError(
                    "resource_conflict", "An unowned GPU process shares a measurement host"
                )

    def install(self) -> dict[str, Any]:
        """Install engine 1's host first, then the other registered hosts."""
        output = self.context.output_dir
        with directory(output, private=True, create=True):
            pass
        bare = output / "source.git"
        bundle = output / "source.bundle"
        commands = [
            ["git", "init", "--bare", str(bare)],
            [
                "git",
                "-C",
                str(bare),
                "fetch",
                "--no-tags",
                self.settings.source_root,
                self.settings.source_commit + ":refs/heads/deployment",
            ],
            ["git", "-C", str(bare), "bundle", "create", str(bundle), "refs/heads/deployment"],
        ]
        for command in commands:
            result = self.context.run_command(
                command, env={"PATH": os.defpath, "GIT_TERMINAL_PROMPT": "0"}
            )
            if result.returncode:
                raise OperationError("command_failed", "Verified source bundle preparation failed")
        bundle.chmod(0o600)
        content = read_input(bundle, maximum=128 * 1024 * 1024)
        import hashlib

        identity = hashlib.sha256(content).hexdigest()
        first = self.host_for("engine-1")
        order = sorted(self.hosts, key=lambda host: (host.id != first, host.id))
        inventories = ssh_prepare.input_document(self.context, self.plan, "host_inventory")
        for host in order:
            self.put(host.id, "source.bundle", content)
            installer = self.put(
                host.id, "install.py", Path(__file__).with_name("ssh_install.py").read_bytes()
            )
            request = self.put(
                host.id,
                "install.json",
                encode_record({"commit": self.settings.source_commit, "bundle_sha256": identity}),
            )
            self.command(
                host.id,
                "install",
                [inventories[host.id]["python_executable"], str(installer), str(request)],
                cwd=self.root,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                kind="installation",
            )
            self.state["checkouts"][host.id] = str(self.root / "checkout")
            self.save()
        return {
            "source_commit": self.settings.source_commit,
            "bundle_sha256": identity,
            "host_order": [host.id for host in order],
        }

    def cleanup_current(self) -> None:
        """Cancel current-stage jobs within their recorded cleanup budget."""
        context = self.context
        end = min(
            context.hard_deadline,
            time.monotonic()
            + sum(
                context.stage["cleanup"][key]
                for key in ("term_grace_ms", "kill_grace_ms", "reconcile_ms")
            )
            / 1000,
        )
        row = next(
            stage for stage in context.read()["stages"] if stage["stage_id"] == context.stage_id
        )
        for effect in row["effects"]:
            if effect["host_id"] == "management" or effect["effect"] == "absent":
                continue
            if effect in self.state.get("services", []):
                continue
            job_id = effect["owner"]["launch_token"]
            with contextlib.suppress(OperationError):
                self.transport.cancel_owned(effect["host_id"], job_id, deadline=end)
                while True:
                    result = self.transport.status_owned(effect["host_id"], job_id, deadline=end)
                    if result.get("owner") != effect["owner"]:
                        raise OperationError("ownership_conflict", "Cleanup receipt changed owner")
                    if result.get("state") in _FINISHED or time.monotonic() >= end:
                        break
                    time.sleep(min(0.2, max(0, end - time.monotonic())))
                if remote_absent(result):
                    effect["effect"] = "absent"
                    effect["identity"] = {"job_id": job_id, "receipt": result}
                    context.record_effect(effect)


class SSHAdapter:
    """Keep deployment orchestration and ownership behind registered actions."""

    @property
    def manifest(self) -> AdapterManifest:
        """Return the verified installed SSH adapter identity."""
        return ssh_prepare.manifest()

    def prepare(
        self,
        context: StageContext,
        target: ManagementTarget,
        action: str,
        parameters: dict[str, Any],
    ) -> PreparedPlan:
        """Bind the requested action to observed inputs and fixed stage order."""
        return ssh_prepare.prepare(context, target, action, parameters)

    def check(self, context: StageContext, target: ManagementTarget, plan: dict[str, Any]) -> None:
        """Check retained bindings and all surviving engine generations."""
        ssh_prepare.check(context, target, plan)
        Session(context, plan).check_generations()

    def execute_stage(
        self, context: StageContext, stage: dict[str, Any], plan: dict[str, Any]
    ) -> StageOutcome:
        """Run one required stage and export its redacted result."""
        from .ssh_workflow import execute

        session = Session(context, plan)
        try:
            data = execute(session, stage["operation"])
            session.state["gates"][stage["stage_id"]] = data
            session.save()
            store = ArtifactStore(str(context.registry.registry_id), context.target)
            reference = store.export(
                encode_record(
                    public_value(
                        context.access,
                        context.target,
                        {
                            "operation_id": context.operation_id,
                            "stage_id": stage["stage_id"],
                            "gate": stage["gate"],
                            "data": data,
                        },
                    )
                ),
                "deployment_gate",
            )
            references = {
                row["artifact_id"]: row
                for row in [
                    *getattr(session, "artifacts", []),
                    *data.get("artifacts", []),
                    reference,
                ]
            }
            return StageOutcome(data=data, artifacts=list(references.values()))
        except BaseException:
            session.cleanup_current()
            raise

    def reconcile(self, context: StageContext, operation: dict[str, Any]) -> ReconcileOutcome:
        """Inspect remote job ownership without restarting or stopping resources."""
        plan = context.store.plan(context.target_id, context.operation_id)
        if plan is None:
            return ReconcileOutcome(helpers_stopped=True, complete=True)
        session = Session(context, plan)
        cleanup = plan["payload"]["action"] == "deployment_cleanup"
        effects = []
        errors = []
        stopped = True
        if cleanup:
            from .ssh_workflow import cleanup_effects

            selected = ssh_prepare.input_document(context, plan, "cleanup_selection")
            recorded = {
                (effect["resource_id"], encode_record(effect["owner"]))
                for stage in operation["stages"]
                for effect in stage["effects"]
            }
            if any(
                (effect["resource_id"], encode_record(effect["owner"])) not in recorded
                for effect in cleanup_effects(selected)
            ):
                stopped = False
                errors.append(
                    {
                        "code": "recovery_required",
                        "message": "Cleanup did not record every selected remote resource",
                    }
                )
        for stage in operation["stages"]:
            for previous in stage["effects"]:
                if previous["host_id"] == "management":
                    continue
                effect = dict(previous)
                try:
                    row = session.transport.status(
                        effect["host_id"], effect["owner"]["launch_token"]
                    )
                    if row["owner"] != effect["owner"]:
                        raise OperationError(
                            "ownership_conflict", "Remote ownership receipt changed"
                        )
                    effect["identity"] = {"job_id": row["job_id"], "receipt": row}
                    effect["effect"] = (
                        "absent"
                        if remote_absent(row)
                        else "confirmed"
                        if row["state"] == "retained" and not cleanup
                        else "unknown"
                    )
                except OperationError as error:
                    effect["effect"] = "unknown"
                    errors.append({"code": error.code, "message": error.message})
                if effect["kind"] == "measurement_helper" and effect["effect"] != "absent":
                    stopped = False
                effects.append(effect)
        return ReconcileOutcome(
            helpers_stopped=stopped,
            complete=not errors and all(row["effect"] != "unknown" for row in effects),
            effects=effects,
            errors=errors,
        )
