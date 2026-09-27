"""Exercise the installed dev adapter with explicit CPU substitutions outside a checkout."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from mcp import Client, StdioServerParameters, stdio_client

import narwhal
from narwhal.deployment import native_engine, stages
from narwhal.dev import template


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def ports() -> int:
    for base in range(21000, 50000, 317):
        sockets = []
        try:
            for offset in (
                0,
                1,
                2,
                101,
                102,
                201,
                202,
                301,
                302,
                1000,
                1001,
                1002,
                1101,
                1102,
                1201,
                1202,
            ):
                selected = socket.socket()
                sockets.append(selected)
                selected.bind(("127.0.0.1", base + offset))
            return base
        except OSError:
            continue
        finally:
            for selected in sockets:
                selected.close()
    raise AssertionError("CPU fixture could not reserve a port layout")


async def call(client, name, arguments):
    reply = await client.call_tool(name, arguments)
    value = reply.structured_content
    assert value is not None, reply
    return value


async def artifact_document(client, artifact_id):
    offset = 0
    chunks = []
    while True:
        result = await call(
            client,
            "artifact_read",
            {"target_id": "dev", "artifact_id": artifact_id, "offset": offset},
        )
        assert result["outcome"] == "success", result
        page = result["data"]
        chunks.append(page["text"])
        if page["next_offset"] is None:
            assert page["complete"], page
            return json.loads("".join(chunks))
        assert page["next_offset"] > offset, page
        offset = page["next_offset"]


async def terminal(client, operation_id, *, expected="succeeded", timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await call(
            client, "operation_inspect", {"target_id": "dev", "operation_id": operation_id}
        )
        assert result["outcome"] == "success", result
        record = result["data"]["operation"]
        if record["state"] in {"succeeded", "failed", "cancelled"}:
            assert record["state"] == expected, record
            return record
        await asyncio.sleep(0.1)
    raise AssertionError(record)


async def prepare(client, action, *, target="dev", parameters=None):
    result = await call(
        client,
        "plan_prepare",
        {
            "target_id": target,
            "action": action,
            "parameters": parameters or {},
            "request_id": str(uuid4()),
            "timeout_s": 1,
        },
    )
    assert result["outcome"] == "accepted", result
    if target == "dev":
        record = await terminal(client, result["data"]["operation_id"])
    else:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            view = await call(
                client,
                "operation_inspect",
                {"target_id": target, "operation_id": result["data"]["operation_id"]},
            )
            record = view["data"]["operation"]
            if record["state"] in {"succeeded", "failed", "cancelled"}:
                assert record["state"] == "succeeded", record
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(record)
    return record["result_data"]["plan_id"]


async def execute(client, plan_id, *, request_id=None, target="dev"):
    result = await call(
        client,
        "plan_execute",
        {
            "target_id": target,
            "plan_id": plan_id,
            "request_id": request_id or str(uuid4()),
            "timeout_s": 1,
        },
    )
    assert result["outcome"] == "accepted", result
    return result["data"]["operation_id"]


async def wait_file(path):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"CPU fixture did not reach {path.name}")


async def check(root: Path, source: Path, *, fixture_provenance: bool) -> None:
    bootstrap = root / "bootstrap"
    bootstrap.mkdir(mode=0o700)
    shutil.copyfile(source / "tests/mcp/local_dev_cpu.py", bootstrap / "local_dev_cpu.py")
    (bootstrap / "sitecustomize.py").write_text("from local_dev_cpu import install\ninstall()\n")
    model = root / "model"
    model.mkdir(mode=0o700)
    (model / "cpu.gguf").write_bytes(b"CPU fixture model identity")
    write(model / "config.json", {})
    recipe = template.default_template()
    recipe["name"] = "cpu-lifecycle-fixture"
    recipe["model"].update(
        filename="cpu.gguf",
        sha256=hashlib.sha256((model / "cpu.gguf").read_bytes()).hexdigest(),
        tokenizer_sha256={},
    )
    base = ports()
    recipe["ports"]["ucx_range"] = f"{base + 301}-{base + 302}"
    recipe_path = root / "recipe.json"
    write(recipe_path, recipe)
    targets = []
    for name, offset in (("dev", 0), ("other", 1000), ("legacy", 2000)):
        settings = root / f"{name}-settings.json"
        write(
            settings,
            {
                "schema": "narwhal.local-dev-settings",
                "schema_version": 1,
                "init": {
                    "model_path": str(model / "cpu.gguf"),
                    "model_dir": str(model),
                    "gpu_uuid": "GPU-cpu-fixture",
                    "fabric_interface": "lo",
                    "port_base": base + offset,
                },
                "budgets": {
                    action: {
                        "timeout_ms": 30000,
                        "term_grace_ms": 1000,
                        "kill_grace_ms": 1000,
                        "reconcile_ms": 3000,
                    }
                    for action in ("dev_init", "dev_up", "dev_verify", "dev_down")
                },
            },
        )
        targets.append(
            {
                "id": name,
                "kind": "dev",
                "working_directory": str(root),
                "artifact_root": str(root / f"{name}-artifacts"),
                "instance_dir": str(root / name),
                "fleet_file": None,
                "adapter": {"id": "local-dev-v1", "settings_path": str(settings)},
                "capabilities": ["inspect", "measure", "mutate"],
                "actions": ["dev_init", "dev_up", "dev_verify", "dev_down"],
                "recipes": [{"id": "cpu", "kind": "dev", "path": str(recipe_path)}],
            }
        )
    registry = root / "registry.json"
    write(
        registry,
        {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": str(uuid4()),
            "state_dir": str(root / "state"),
            "targets": targets,
        },
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(bootstrap),
        "NARWHAL_MANAGEMENT_REGISTRY": str(registry),
    }
    if fixture_provenance:
        environment["NARWHAL_CPU_FAKE_PROVENANCE"] = "1"
    unbound = {
        key: value for key, value in environment.items() if key != "NARWHAL_MANAGEMENT_REGISTRY"
    }
    legacy = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "narwhal.dev.cli",
        "dev",
        "init",
        "--instance",
        str(root / "legacy"),
        "--template",
        str(recipe_path),
        "--model",
        str(model / "cpu.gguf"),
        "--model-dir",
        str(model),
        "--gpu",
        "GPU-cpu-fixture",
        "--interface",
        "lo",
        "--port-base",
        str(base + 2000),
        "--format",
        "json",
        env=unbound,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(legacy.communicate(), 20)
    assert legacy.returncode == 0, (stdout, stderr)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "narwhal.mcp.cli", "--registry", str(registry)],
        cwd=root,
        env=environment,
    )

    @contextlib.asynccontextmanager
    async def client():
        with (root / "mcp.stderr").open("a") as stderr:
            async with Client(
                stdio_client(parameters, errlog=stderr), mode="legacy", read_timeout_seconds=15
            ) as selected:
                yield selected

    async with client() as selected:
        tools = await selected.list_tools()
        assert len(tools.tools) == 14, tools
        legacy_status = await call(selected, "dev_status", {"target_id": "legacy"})
        assert legacy_status["outcome"] == "success", legacy_status
        assert legacy_status["data"]["status"] == "stopped", legacy_status
        init_plan = await prepare(selected, "dev_init", parameters={"recipe_id": "cpu"})
        await terminal(selected, await execute(selected, init_plan))
        other_plan = await prepare(
            selected, "dev_init", target="other", parameters={"recipe_id": "cpu"}
        )
        up_plan = await prepare(selected, "dev_up")
        request_id = str(uuid4())
        (root / "hold-up").touch()
        operation_id = await execute(selected, up_plan, request_id=request_id)
        await wait_file(root / "up-started")
        duplicate = await execute(selected, up_plan, request_id=request_id)
        assert duplicate == operation_id
        conflict = await call(
            selected,
            "plan_execute",
            {"target_id": "other", "plan_id": other_plan, "request_id": str(uuid4())},
        )
        assert (
            conflict["outcome"] == "failed_gate"
            and conflict["errors"][0]["code"] == "resource_busy"
        ), conflict
        cli = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "narwhal.dev.cli",
            "dev",
            "up",
            "--instance",
            str(root / "dev"),
            "--format",
            "json",
            env=environment,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(cli.communicate(), 20)
        outcome = json.loads(stdout)
        assert cli.returncode != 0 and outcome["status"] != "success", (outcome, stderr)
    # The stdio process has exited while its detached operation still owns work.
    async with client() as selected:
        duplicate = await execute(selected, up_plan, request_id=request_id)
        assert duplicate == operation_id
        (root / "hold-up").unlink()
        await terminal(selected, operation_id)
        status = await call(selected, "dev_status", {"target_id": "dev"})
        assert status["outcome"] == "success", status
        assert status["data"]["status"] == "launched", status
        (root / "busy-state").touch()
        busy = await call(
            selected,
            "plan_prepare",
            {
                "target_id": "dev",
                "action": "dev_verify",
                "parameters": {},
                "request_id": str(uuid4()),
                "timeout_s": 1,
            },
        )
        assert busy["outcome"] == "accepted", busy
        rejected = await terminal(selected, busy["data"]["operation_id"], expected="failed")
        assert "fleet_busy" in rejected["error_codes"], rejected
        assert not list((root / "dev").glob("run-*/verify-*"))
        (root / "busy-state").unlink()
        verify = await prepare(selected, "dev_verify")
        await terminal(selected, await execute(selected, verify))
        status = await call(selected, "dev_status", {"target_id": "dev"})
        assert status["outcome"] == "success", status
        assert status["data"]["status"] == "ready", status
        (root / "fail-verify").touch()
        verify = await prepare(selected, "dev_verify")
        failed_id = await execute(selected, verify)
        failed = await terminal(selected, failed_id, expected="failed")
        assert failed["result_status"] == "invalid_input", failed
        assert "invalid_input" in failed["error_codes"], failed
        view = await call(
            selected, "operation_inspect", {"target_id": "dev", "operation_id": failed_id}
        )
        assert view["outcome"] == "success", view
        retained = await artifact_document(selected, view["data"]["record_artifact_id"])
        command = retained["result"]["command_result"]
        assert command == retained["stages"][-1]["command_result"], retained
        assert command["operation"] == "dev verify", command
        assert command["status"] == "invalid_input" and command["exit_code"] == 2, command
        assert command["errors"] and retained["result"]["errors"], retained
        assert "routed arithmetic canary expected 5" in command["errors"][0]["message"], command
        status = await call(selected, "dev_status", {"target_id": "dev"})
        assert status["outcome"] == "degraded", status
        assert status["data"]["status"] == "degraded", status
        assert status["errors"] and status["command_result"]["status"] == "degraded", status
        (root / "fail-verify").unlink()
        down = await prepare(selected, "dev_down")
        await terminal(selected, await execute(selected, down))
        up = await prepare(selected, "dev_up")
        (root / "up-started").unlink()
        (root / "hold-up").touch()
        cancelled = await execute(selected, up)
        await wait_file(root / "up-started")
        await call(selected, "operation_cancel", {"target_id": "dev", "operation_id": cancelled})
        await terminal(selected, cancelled, expected="cancelled")
        (root / "hold-up").unlink()
        down = await prepare(selected, "dev_down")
        await terminal(selected, await execute(selected, down))
        status = await call(selected, "dev_status", {"target_id": "dev"})
        assert status["outcome"] == "success", status
        assert status["data"]["status"] == "stopped", status
    print(
        "Installed MCP dev lifecycle CPU fixture passed: lifecycle, reconnect, "
        "deduplication, exclusion, busy-router refusal, verification failure "
        "and partial-startup cancellation"
    )


def cleanup(root: Path) -> None:
    for path in root.glob("dev/run-*/engine-*/native-process.json"):
        with contextlib.suppress(OSError, ValueError):
            stages._signal(
                native_engine._group_members(json.loads(path.read_text())), signal.SIGKILL
            )
    state = root / "dev/lifecycle.json"
    if state.exists():
        for row in json.loads(state.read_text()).get("processes", []):
            with contextlib.suppress(OSError, ValueError):
                stages._signal(native_engine._group_members(row["identity"]), signal.SIGKILL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--fixture-provenance",
        action="store_true",
        help="prototype only; final installed checks must omit this option",
    )
    args = parser.parse_args()
    source = args.source_root.resolve()
    if not args.fixture_provenance:
        assert not Path(narwhal.__file__).resolve().is_relative_to(source), narwhal.__file__
    with contextlib.ExitStack() as stack:
        root = (
            args.output.resolve()
            if args.output
            else Path(stack.enter_context(tempfile.TemporaryDirectory()))
        )
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            asyncio.run(check(root, source, fixture_provenance=args.fixture_provenance))
        finally:
            cleanup(root)


if __name__ == "__main__":
    main()
