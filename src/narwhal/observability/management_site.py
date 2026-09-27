"""Inspect registered local or SSH sites without admitting deployment actions."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from narwhal.deployment import ssh_settings
from narwhal.deployment.management_access import AccessError, InspectionAccess, now, read_input
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementTarget
from narwhal.deployment.ssh_prepare import deployment_state
from narwhal.deployment.ssh_transport import inspect_rpc

from .management_http import origin
from .management_local import LocalSiteProvider
from .management_targets import build_targets
from .management_types import MonitoringBinding, SourceCapture


class RegisteredSiteProvider:
    """Select installed inspection implementations using registered target adapters."""

    def __init__(self, access: InspectionAccess) -> None:
        self.access = access
        self.local = LocalSiteProvider(access)

    def _inputs(
        self, target: ManagementTarget, deadline: float
    ) -> tuple[ssh_settings.SSHSettings, tuple[ssh_settings.SSHHost, ...], dict[str, str]]:
        if target.kind != "fleet" or target.adapter.id != "ssh-v1":
            raise AccessError("adapter_unavailable", "Target requires an installed site adapter")
        if monotonic() >= deadline:
            raise AccessError("source_unavailable", "Site inspection deadline expired")
        settings = ssh_settings.load(target)
        try:
            inventory = ssh_settings.HostInventory.model_validate_json(
                read_input(Path(settings.hosts_path))
            )
        except AccessError:
            raise
        except ValueError:
            raise AccessError("invalid_input", "Registered SSH host inventory is invalid") from None
        names = set()
        for host in inventory.hosts:
            names.add(host.ssh_env)
            if host.password_env:
                if host.password_env not in target.credential_env:
                    raise AccessError(
                        "permission_denied", "SSH credential reference is not registered"
                    )
                names.add(host.password_env)
        values = {name: os.environ[name] for name in names if name in os.environ}
        return settings, ssh_settings.load_hosts(settings, values), values

    async def monitoring_binding(
        self, target: ManagementTarget, *, deadline: float
    ) -> MonitoringBinding:
        """Check saved service ownership and reconstruct host-local scrape expectations."""
        if target.kind == "dev":
            return await self.local.monitoring_binding(target, deadline=deadline)
        settings, hosts, values = self._inputs(target, deadline)
        state = deployment_state(target, registry_id=str(self.access.registry.registry_id))
        try:
            monitoring = state["monitoring"]
            effect = monitoring["effect"]
            host = next(
                item
                for item in hosts
                if item.id == monitoring["host_id"] and "router" in item.roles
            )
            if state["router"]["host_id"] != host.id or effect["host_id"] != host.id:
                raise ValueError("Monitoring host differs")
            recipe = ssh_settings.load_recipe(target, state["recipe_id"])
            _, raw_fleet = self.access.fleet(target)
            fleet = copy.deepcopy(raw_fleet)
            environment = self.access.environment(target, fleet)
            environment.update(recipe.environment)
            for field, reference in recipe.environment_refs.items():
                try:
                    ssh_settings.SSHRecipe.nonsecret({field: "reference"})
                except ValueError:
                    if reference not in target.credential_env:
                        raise AccessError(
                            "permission_denied", "Recipe credential reference is not registered"
                        ) from None
                if value := os.environ.get(reference):
                    environment[field] = value
            for engine in fleet["engines"]:
                if match := re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", engine["url"]):
                    engine["url"] = environment[match[1]]
            expected = build_targets(fleet, origin(state["router"]["url"]))
            binding = monitoring["binding"]
            if (
                binding["targets"]["router"] != expected.router
                or tuple(tuple(row) for row in binding["targets"]["engines"]) != expected.engines
                or binding["host_id"] != host.id
                or origin(binding["datasource_url"]) != origin(monitoring["prometheus_url"])
            ):
                raise AccessError(
                    "stale_plan", "Monitoring binding differs from registered fleet inputs"
                )
            status = await inspect_rpc(
                settings,
                host,
                values,
                {"command": "status", "job_id": effect["owner"]["launch_token"]},
                deadline=deadline,
            )
            if (
                status["owner"] != effect["owner"]
                or status["state"] != "retained"
                or not status.get("supervisor_present")
            ):
                raise AccessError(
                    "source_unavailable", "Monitoring supervisor is absent or changed"
                )
            observed = await inspect_rpc(
                settings,
                host,
                values,
                {"command": "inspect_containers", "job_id": effect["owner"]["launch_token"]},
                deadline=deadline,
            )
            expected_ids = {row["cid"] for row in monitoring["containers"].values()}
            if (
                len(expected_ids) != 2
                or {row["container_id"] for row in observed["containers"]} != expected_ids
                or any(not row["running"] for row in observed["containers"])
            ):
                raise AccessError(
                    "source_unavailable", "Monitoring containers are absent or changed"
                )
            return MonitoringBinding(
                expected,
                binding["datasource_url"],
                host.id,
                binding["prometheus_version"],
                binding["grafana_version"],
            )
        except (AccessError, OperationError):
            raise
        except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
            raise AccessError(
                "source_unavailable", "Recorded monitoring binding is unavailable or invalid"
            ) from None

    async def collect(
        self,
        target: ManagementTarget,
        kind: Literal["inventory", "log"],
        subject_id: str,
        *,
        deadline: float,
        max_bytes: int,
    ) -> tuple[SourceCapture, ...]:
        """Inspect declared SSH hosts or regular log tails under the registered content policy."""
        if target.kind == "dev":
            return await self.local.collect(
                target, kind, subject_id, deadline=deadline, max_bytes=max_bytes
            )
        settings, hosts, values = self._inputs(target, deadline)
        observed = now()
        if kind == "log":
            log = next((row for row in target.logs if row.id == subject_id), None)
            if log is None:
                raise AccessError("invalid_input", "Log is not registered")
            if (
                log.source.name in {"journal.jsonl", "completion.json"}
                and not target.allow_request_content
            ):
                raise AccessError(
                    "permission_denied", "Target does not permit request-content logs"
                )
            selected = log.host_id
            parameters: dict[str, Any] = {"path": str(log.source), "max_bytes": max_bytes}
            probe = "log"
        else:
            selected, parameters, probe = subject_id, {}, "inventory"
        host = next((row for row in hosts if row.id == selected), None)
        if host is None:
            raise AccessError("invalid_input", "Host alias is not registered")
        try:
            value = await inspect_rpc(
                settings,
                host,
                values,
                {"command": "probe", "kind": probe, "parameters": parameters},
                deadline=deadline,
            )
        except OperationError as error:
            return (
                SourceCapture(
                    subject_id,
                    b"",
                    observed,
                    False,
                    "unavailable",
                    error.code,
                    "Registered host source is unavailable",
                ),
            )
        provenance: dict[str, Any] = {"host_id": host.id}
        if kind == "log":
            content = base64.b64decode(value["data_base64"], validate=True)
            complete = value["complete"]
            provenance.update(offset=value["offset"], size_bytes=value["size_bytes"])
        else:
            content = json.dumps(value, allow_nan=False).encode()
            complete = value.get("gpu_clients", {}).get("complete") is True
        status = "ok" if complete else "unavailable"
        error_code = None if complete else "source_unavailable"
        redactor = self.access.redactor(target)
        try:
            content = redactor.body(content)
        except (ValueError, UnicodeError, RecursionError):
            return (SourceCapture(subject_id, b"", observed, False, "error", "invalid_source"),)
        if len(content) > max_bytes:
            content, complete = content[:max_bytes].decode("utf-8", errors="ignore").encode(), False
            status, error_code = "truncated", "source_truncated"
        return (
            SourceCapture(
                subject_id,
                content,
                observed,
                complete,
                status,
                error_code,
                provenance=provenance,
            ),
        )
