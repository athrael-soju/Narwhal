"""Derive engine attestation and the router contract from checked deployment evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from narwhal.config import EngineContract, FleetConfig
from narwhal.engines.attestation import (
    AttestationDocument,
    fetch_engine_identity,
    parse_process_start,
    verify_attestation,
)
from narwhal.engines.attestation import (
    main as attest_main,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def write_private_text(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(data)


def write_private(path: Path, value: dict) -> None:
    write_private_text(path, json.dumps(value, indent=2) + "\n")


def checked_plan(run: Path) -> tuple[dict, dict, str]:
    plan_path = run / "launch.json"
    plan = read_json(plan_path)
    checked = read_json(run / "checked.json")
    plan_hash = digest(plan_path)
    if checked.get("plan_sha256") != plan_hash:
        raise ValueError("Image check belongs to another serving plan")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", checked.get("image_id", "")):
        raise ValueError("Image check lacks an immutable image ID")
    return plan, checked, plan_hash


def live_container(run: Path, checked: dict) -> str:
    cid = (run / "container.id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("Serving container ID is invalid")
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .}}", cid],
        capture_output=True,
        text=True,
        check=True,
    )
    observed = json.loads(result.stdout)
    if observed.get("Id") != cid or observed.get("Image") != checked["image_id"]:
        raise ValueError("Serving container differs from the checked image or recorded ID")
    if observed.get("State", {}).get("Running") is not True:
        raise ValueError("Serving container must be running before attestation")
    return cid


NIXL_CAPTURE_TAG = "NARWHAL_NIXL_CAPTURE_V1:"
NIXL_CAPTURE = f"""import contextlib, hashlib, importlib, json, sys
from pathlib import Path
with contextlib.redirect_stdout(sys.stderr):
    module = importlib.import_module("vllm.distributed.kv_transfer.kv_connector.v1.nixl.metadata")
version = module.NIXL_CONNECTOR_VERSION
if type(version) is not int or version < 1:
    raise ValueError("NIXL_CONNECTOR_VERSION must be a positive integer")
source = Path(module.__file__)
record = {{"nixl_connector_version": version, "module": module.__name__,
          "constant": "NIXL_CONNECTOR_VERSION", "module_file": str(source),
          "module_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}}
print("\\n" + {NIXL_CAPTURE_TAG!r} + json.dumps(record), flush=True)
"""


def parse_tagged_capture(output: str, tag: str, label: str) -> dict:
    captures = [line.removeprefix(tag) for line in output.splitlines() if line.startswith(tag)]
    if len(captures) != 1:
        raise ValueError(f"Expected one tagged {label} capture, received {len(captures)}")
    try:
        record = json.loads(captures[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Tagged {label} capture contains invalid JSON") from exc
    if not isinstance(record, dict):
        raise ValueError(f"Tagged {label} capture must contain a JSON object")
    return record


def parse_nixl_capture(output: str) -> dict:
    return parse_tagged_capture(output, NIXL_CAPTURE_TAG, "NIXL")


MODEL_DIMENSIONS_CAPTURE_TAG = "NARWHAL_LIVE_MODEL_DIMENSIONS_V1:"
MODEL_DIMENSIONS_CAPTURE = f"""import hashlib, json, sys
from pathlib import Path
import launch_engine
plan_path = Path(sys.argv[2])
plan_data = plan_path.read_bytes()
plan_hash = hashlib.sha256(plan_data).hexdigest()
if plan_hash != sys.argv[1]:
    raise ValueError("mounted launch plan differs from the checked serving plan")
plan = json.loads(plan_data)
launcher_hash = hashlib.sha256(Path(launch_engine.__file__).read_bytes()).hexdigest()
if launcher_hash != plan["launcher_sha256"]:
    raise ValueError("mounted launcher differs from the checked serving plan")
model_hash = hashlib.sha256(Path(sys.argv[3]).read_bytes()).hexdigest()
if model_hash != plan["model_config_sha256"]:
    raise ValueError("mounted model configuration differs from the checked serving plan")
model = launch_engine.runtime_config(plan).model_config
methods = {{"head_size": "get_head_size", "kv_heads": "get_total_num_kv_heads",
           "hidden_layers": "get_total_num_hidden_layers"}}
values = {{field: getattr(model, method)() for field, method in methods.items()}}
if any(type(value) is not int or value < 1 for value in values.values()):
    raise ValueError("model contract getters must return positive integers")
architecture = model.architecture
if not isinstance(architecture, str) or not architecture.strip():
    raise ValueError("runtime model architecture is empty")
record = {{"contract": values,
          "sources": {{field: f"ModelConfig.{{method}}()" for field, method in methods.items()}},
          "model_architecture": architecture, "use_mla": model.use_mla,
          "model_config_sha256": model_hash, "plan_sha256": plan_hash,
          "launcher_sha256": launcher_hash, "image": plan["image"],
          "revision": plan["revision"]}}
print("\\n" + {MODEL_DIMENSIONS_CAPTURE_TAG!r} + json.dumps(record), flush=True)
"""


def capture_model_dimensions(run: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    cid = live_container(run, checked)
    destination = run / "model-dimensions.live.json"
    if destination.exists():
        raise ValueError("Live model dimensions already exist; retain the capture")
    result = subprocess.run(
        [
            "docker",
            "exec",
            "--env",
            "NARWHAL_CAPTURE_CACHE=0",
            cid,
            "python3",
            "-c",
            MODEL_DIMENSIONS_CAPTURE,
            plan_hash,
            "/narwhal-hooks/launch.json",
            "/model/config.json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    log = run / f"model-dimensions.live-{uuid.uuid4().hex}.log"
    write_private_text(log, result.stdout + "\nSTDERR\n" + result.stderr)
    if result.returncode:
        raise ValueError(f"Live model dimension inspection failed; inspect {log}")
    try:
        record = parse_tagged_capture(
            result.stdout, MODEL_DIMENSIONS_CAPTURE_TAG, "model dimensions"
        )
    except ValueError as exc:
        raise ValueError(f"{exc}; inspect {log}") from exc
    require_binding(
        record,
        "Live model dimensions",
        plan_sha256=plan_hash,
        image=plan["image"],
        revision=plan["revision"],
        model_config_sha256=plan["model_config_sha256"],
        launcher_sha256=plan["launcher_sha256"],
    )
    if (
        not isinstance(record.get("model_architecture"), str)
        or not record["model_architecture"].strip()
    ):
        raise ValueError("Live model dimensions lack the resolved architecture")
    contract = record.get("contract")
    if not isinstance(contract, dict) or any(
        type(contract.get(field)) is not int or contract[field] < 1
        for field in ("head_size", "kv_heads", "hidden_layers")
    ):
        raise ValueError("Live model dimensions contain invalid compatibility getters")
    original = run / "model-dimensions.json"
    if original.exists():
        previous = read_json(original)
        require_binding(
            previous,
            "Prior model dimensions",
            plan_sha256=plan_hash,
            image=plan["image"],
            revision=plan["revision"],
            model_config_sha256=plan["model_config_sha256"],
        )
        if previous.get("contract") != contract:
            raise ValueError("Live model dimensions differ from the retained plan capture")
    record.update(image_id=checked["image_id"], container_id=cid, capture_log_sha256=digest(log))
    write_private(destination, record)
    return destination


def capture_nixl(run: Path) -> Path:
    _, checked, plan_hash = checked_plan(run)
    cid = live_container(run, checked)
    result = subprocess.run(
        ["docker", "exec", cid, "python3", "-c", NIXL_CAPTURE],
        capture_output=True,
        text=True,
        check=True,
    )
    record = parse_nixl_capture(result.stdout)
    if (
        type(record.get("nixl_connector_version")) is not int
        or record["nixl_connector_version"] < 1
    ):
        raise ValueError("Pinned connector returned an invalid protocol version")
    record.update(plan_sha256=plan_hash, image_id=checked["image_id"], container_id=cid)
    destination = run / "nixl-connector-version.json"
    write_private(destination, record)
    return destination


def require_binding(record: dict, label: str, **expected: object) -> None:
    for field, value in expected.items():
        if record.get(field) != value:
            raise ValueError(f"{label} {field} differs from the checked serving plan")


def option(args: list[str], name: str) -> str:
    if args.count(name) != 1:
        raise ValueError(f"Serving plan requires one {name} argument")
    index = args.index(name)
    if index + 1 == len(args):
        raise ValueError(f"Serving plan has no value for {name}")
    return args[index + 1]


def attention_backends(log: str) -> str:
    names = set()
    for pattern in (
        r"\bUsing (?:AttentionBackendEnum\.)?([A-Za-z][A-Za-z0-9_]*) (?:attention )?backend\b",
        r"\bOverriding with ([A-Za-z][A-Za-z0-9_]*) out of potential backends\b",
    ):
        names.update(re.findall(pattern, log))
    if not names:
        raise ValueError(
            "Serving startup log has no resolved attention backend; retain the complete Docker log"
        )
    return ",".join(sorted(names))


def engine_document(run: Path, startup_log: Path) -> dict:
    plan, checked, plan_hash = checked_plan(run)
    cid = live_container(run, checked)
    inspection = run
    inspection_hash = plan_hash
    original_dimensions = inspection / "model-dimensions.json"
    live_dimensions = inspection / "model-dimensions.live.json"
    dimension_source = live_dimensions if live_dimensions.exists() else original_dimensions
    dimensions = read_json(dimension_source)
    registration = read_json(inspection / "cache-registration.json")
    require_binding(
        dimensions,
        "Model dimensions",
        plan_sha256=inspection_hash,
        image=plan["image"],
        model_config_sha256=plan["model_config_sha256"],
        revision=plan["revision"],
    )
    if dimension_source == live_dimensions:
        require_binding(
            dimensions,
            "Live model dimensions",
            image_id=checked["image_id"],
            container_id=cid,
            launcher_sha256=plan["launcher_sha256"],
        )
        if original_dimensions.exists():
            previous = read_json(original_dimensions)
            require_binding(
                previous,
                "Prior model dimensions",
                plan_sha256=plan_hash,
                image=plan["image"],
                model_config_sha256=plan["model_config_sha256"],
                revision=plan["revision"],
            )
            if previous.get("contract") != dimensions.get("contract"):
                raise ValueError("Live model dimensions differ from the retained plan capture")
    require_binding(
        registration, "Cache registration", plan_sha256=inspection_hash, image=plan["image"]
    )
    cache = read_json(run / "cache-layout.json")
    require_binding(
        cache,
        "Live cache layout",
        plan_sha256=plan_hash,
        image=plan["image"],
        model_config_sha256=plan["model_config_sha256"],
        launch_config_sha256=plan["launch_sha256"],
    )
    nixl = read_json(run / "nixl-connector-version.json")
    require_binding(
        nixl, "NIXL protocol", plan_sha256=plan_hash, image_id=checked["image_id"], container_id=cid
    )
    transfer = read_json(run / "transfer-mode.json")
    require_binding(transfer, "Transfer mode", plan_sha256=plan_hash, image_id=checked["image_id"])
    handshake = read_json(run / "handshake-policy.json")
    require_binding(
        handshake,
        "Handshake policy",
        plan_sha256=plan_hash,
        image=plan["image"],
        enforce_handshake_compat=True,
    )
    if handshake.get("connector_config") != plan["connector"]:
        raise ValueError("Handshake policy uses another connector configuration")
    model_path = Path(plan["model_dir"]) / "config.json"
    if digest(model_path) != plan["model_config_sha256"]:
        raise ValueError("Model configuration changed after launch preparation")
    architecture = dimensions.get("model_architecture")
    if not isinstance(architecture, str) or not architecture.strip():
        raise ValueError("Capture live model dimensions for the resolved architecture")
    version = read_json(run / "version.json").get("version")
    if version != checked.get("vllm_api_version"):
        raise ValueError("HTTP version differs from the checked image")
    parse_process_start((run / "metrics.txt").read_text())
    ranks = cache.get("ranks", [])
    tp = int(option(plan["args"], "--tensor-parallel-size"))
    if sorted(rank.get("rank") for rank in ranks) != list(range(tp)):
        raise ValueError("Live cache layout lacks a TP rank")
    kinds = [{layer["kind"] for layer in rank["layers"]} for rank in ranks]
    if any(group != kinds[0] for group in kinds):
        raise ValueError("Cache kinds differ between TP ranks")
    if (
        registration["kv_cache_layout"] not in {rank.get("kv_cache_layout") for rank in ranks}
        or len({rank.get("kv_cache_layout") for rank in ranks}) != 1
    ):
        raise ValueError("Resolved cache registration differs from the live cache layout")
    packages = plan["expected_packages"]
    nixl_version = packages.get("nixl") or packages.get("nixl-rocm")
    if not isinstance(nixl_version, str) or not nixl_version:
        raise ValueError("Checked image lacks a pinned NIXL package")
    contract = {
        "vllm_version": version,
        "image_digest": checked["image_id"],
        "nixl_version": nixl_version,
        "nixl_connector_version": nixl["nixl_connector_version"],
        "model_architecture": architecture,
        "model_dtype": option(plan["args"], "--dtype"),
        **dimensions["contract"],
        "attention_backend": attention_backends(startup_log.read_text()),
        "kv_cache_dtype": option(plan["args"], "--kv-cache-dtype"),
        "cross_layers_blocks": registration["cross_layers_blocks"],
        "hybrid_kv_cache_manager": len(kinds[0]) > 1,
        "connector": plan["connector"]["kv_connector"],
        "kv_role": plan["connector"]["kv_role"],
        "transfer_mode": transfer["transfer_mode"],
        "speculative_config": "disabled",
        "enforce_handshake_compat": handshake["enforce_handshake_compat"],
    }
    if set(contract) != set(EngineContract().fields()):
        raise ValueError("Derived contract fields differ from the attestation schema")
    if contract["transfer_mode"] not in {"pull", "push"}:
        raise ValueError("Resolved NIXL transfer mode must be pull or push")
    declared = EngineContract(**contract)
    if declared.missing():
        raise ValueError("Derived engine contract is incomplete: " + ", ".join(declared.missing()))
    evidence = {
        "vllm_version": run / "version.json",
        "image_digest": run / "checked.json",
        "nixl_version": run / "launch.json",
        "nixl_connector_version": run / "nixl-connector-version.json",
        "model_architecture": dimension_source,
        "model_dtype": run / "launch.json",
        "kv_heads": dimension_source,
        "head_size": dimension_source,
        "hidden_layers": dimension_source,
        "attention_backend": startup_log,
        "kv_cache_dtype": run / "launch.json",
        "cross_layers_blocks": inspection / "cache-registration.json",
        "hybrid_kv_cache_manager": run / "cache-layout.json",
        "connector": run / "launch.json",
        "kv_role": run / "launch.json",
        "transfer_mode": run / "transfer-mode.json",
        "speculative_config": run / "launch.json",
        "enforce_handshake_compat": run / "handshake-policy.json",
    }
    sources = {field: f"{path.resolve()} sha256:{digest(path)}" for field, path in evidence.items()}
    return {
        "schema": "narwhal.attestation",
        "schema_version": 1,
        "contract": contract,
        "sources": sources,
    }


def generate(run: Path, startup_log: Path) -> Path:
    record = engine_document(run, startup_log)
    role = read_json(run / "launch.json")["role"]
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("Serving plan has an invalid engine role")
    destination = run / "engine-attestation.json"
    temporary = destination.with_name(destination.name + f".tmp-{uuid.uuid4().hex}")
    try:
        write_private(temporary, record)
        AttestationDocument.load(temporary)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def serve(run: Path) -> int:
    plan, checked, _ = checked_plan(run)
    live_container(run, checked)
    role = plan.get("role", "")
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("Serving plan has an invalid engine role")
    node = role.split("-")[1]
    destination = run / "engine-attestation.json"
    AttestationDocument.load(destination)
    if read_json(destination) != engine_document(run, run / "startup.log"):
        raise ValueError("Attestation document differs from current serving evidence")
    expected = os.environ.get(f"NARWHAL_NODE_{node}_ATTESTATION_URL", "")
    url = urlsplit(expected)
    if url.scheme != "http" or url.path != "/v1/attestation" or not url.hostname or not url.port:
        raise ValueError(f"NARWHAL_NODE_{node}_ATTESTATION_URL must name the sidecar route")
    return attest_main(
        [
            "--document",
            str(destination),
            "--engine-base",
            plan["endpoint"],
            "--host",
            url.hostname,
            "--port",
            str(url.port),
        ]
    )


def finalize_fleet(path: Path) -> EngineContract:
    if path.is_symlink():
        raise ValueError("Fleet configuration must be a regular private file")
    fleet = FleetConfig.load(path)
    contracts = []
    headers = fleet.engine_headers()
    with httpx.Client(timeout=fleet.health_timeout_s) as client:
        for engine in fleet.engines:
            identity = asyncio.run(
                fetch_engine_identity(engine.url, timeout_s=fleet.health_timeout_s, headers=headers)
            )
            response = client.get(engine.attestation_url)
            response.raise_for_status()
            payload = response.json()
            raw = payload.get("contract")
            if not isinstance(raw, dict):
                raise ValueError(f"{engine.iid}: sidecar returned no contract")
            contract = EngineContract(**raw)
            failures = verify_attestation(payload, contract, identity)
            if failures:
                raise ValueError(f"{engine.iid}: " + "; ".join(failures))
            if contract.missing():
                raise ValueError(
                    f"{engine.iid}: incomplete contract: " + ", ".join(contract.missing())
                )
            contracts.append(contract)
    if not contracts or any(
        contract.fields() != contracts[0].fields() for contract in contracts[1:]
    ):
        raise ValueError("Engine sidecars report different contracts")
    raw_fleet = read_json(path)
    if "engine_contract" in raw_fleet:
        if raw_fleet["engine_contract"] != contracts[0].fields():
            raise ValueError("Existing router contract differs from the live sidecars")
        return contracts[0]
    raw_fleet["engine_contract"] = contracts[0].fields()
    backup = Path("runs") / f"fleet.before-attestation-{uuid.uuid4().hex}.json"
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(backup, read_json(path))
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        write_private(temporary, raw_fleet)
        parsed = FleetConfig.load(temporary)
        if parsed.engine_contract != contracts[0]:
            raise ValueError("Generated router contract failed fleet validation")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return contracts[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture-nixl")
    capture.add_argument("--run", required=True, type=Path)
    dimensions = commands.add_parser("capture-model-dimensions")
    dimensions.add_argument("--run", required=True, type=Path)
    engine = commands.add_parser("generate")
    engine.add_argument("--run", required=True, type=Path)
    engine.add_argument("--startup-log", required=True, type=Path)
    serve_command = commands.add_parser("serve")
    serve_command.add_argument("--run", required=True, type=Path)
    fleet = commands.add_parser("finalize-fleet")
    fleet.add_argument("--fleet", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture-nixl":
            result = capture_nixl(args.run)
            print(f"Captured pinned NIXL protocol in {result}")
        elif args.command == "capture-model-dimensions":
            result = capture_model_dimensions(args.run)
            print(f"Captured live model dimensions in {result}")
        elif args.command == "generate":
            result = generate(args.run, args.startup_log)
            print(f"Generated private engine attestation in {result}")
        elif args.command == "serve":
            return serve(args.run)
        else:
            contract = finalize_fleet(args.fleet)
            print(f"Router fleet contract verified across live sidecars: {contract.fingerprint()}")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        httpx.HTTPError,
    ) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
