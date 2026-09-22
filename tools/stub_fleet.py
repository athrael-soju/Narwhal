#!/usr/bin/env python3
"""Run the split-serving protocol against CPU-only vLLM stubs.

The stubs expose the health, version, metrics, attestation, tokenization, model,
completion, handoff, and streaming routes used by Narwhal. Their timing is
deterministic fixture behaviour. Use them to test router wiring before running
on a real fleet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import socket
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from narwhal.config import EngineContract
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation

# Synthetic prefill and decode timing coefficients for the CPU stubs.
TTFT_A = 2e-8
TTFT_B = 6e-5
TTFT_C = 0.005
TPOT_SLOPE = 3e-6
TPOT_INTERCEPT = 0.012
CHARS_PER_TOKEN = 3.8
STUB_VERSION = "0.0.0-stub"
STUB_CONTRACT = EngineContract(
    vllm_version=STUB_VERSION,
    image_digest="sha256:" + "0" * 64,
    nixl_version="stub",
    nixl_connector_version=1,
    model_architecture="StubForCausalLM",
    model_dtype="float32",
    kv_heads=1,
    head_size=1,
    hidden_layers=1,
    attention_backend="STUB",
    kv_cache_dtype="auto",
    cross_layers_blocks=False,
    hybrid_kv_cache_manager=False,
    connector="NixlConnector",
    kv_role="kv_both",
    transfer_mode="pull",
    speculative_config="disabled",
)
STUB_SOURCES = {
    name: f"stub:{name}"
    for name, value in STUB_CONTRACT.fields().items()
    if isinstance(value, bool) or (value is not None and value != "" and value != 0)
}


def build(iid: str, model: str, version: str = STUB_VERSION) -> FastAPI:
    app = FastAPI(title=f"stub-{iid}")
    resident = {"tokens": 0}
    process_started = time.time()

    def count(body: dict) -> int:
        raw = body.get("prompt")
        if raw is None:
            raw = "".join(str(m.get("content", "")) for m in body.get("messages") or [])
        if isinstance(raw, list):
            return len(raw)
        return max(1, int(len(str(raw)) / CHARS_PER_TOKEN))

    @app.get("/health")
    async def health():
        return {"status": "ok", "iid": iid}

    @app.get("/version")
    async def engine_version():
        return {"version": version}

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(f"process_start_time_seconds {process_started}\n")

    @app.get("/v1/attestation")
    async def attestation():
        return make_attestation(
            AttestationDocument(STUB_CONTRACT, STUB_SOURCES),
            EngineIdentity(version, process_started),
        )

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": model, "object": "model"}]}

    @app.post("/tokenize")
    async def tokenize(request: Request):
        body = await request.json()
        return {"count": count(body), "max_model_len": 32768}

    @app.post("/v1/completions")
    async def completions(request: Request):
        body = await request.json()
        n_in = count(body)
        kv = body.get("kv_transfer_params") or {}

        if kv.get("do_remote_decode"):
            resident["tokens"] += n_in
            try:
                await asyncio.sleep(TTFT_A * n_in * n_in + TTFT_B * n_in + TTFT_C)
            finally:
                resident["tokens"] -= n_in
            return JSONResponse(
                {
                    "id": f"cmpl-{iid}",
                    "object": "text_completion",
                    "choices": [
                        {
                            "index": 0,
                            "text": "",
                            "finish_reason": "length",
                            "kv_transfer_params": {
                                "remote_engine_id": iid,
                                "remote_block_ids": [1, 2, 3],
                                "remote_host": "stub",
                                "remote_port": 0,
                            },
                        }
                    ],
                }
            )

        n_out = int(body.get("max_tokens", 16))
        resident["tokens"] += n_in
        if not kv:
            # No handoff: this instance has to prefill it itself.
            await asyncio.sleep(TTFT_A * n_in * n_in + TTFT_B * n_in + TTFT_C)

        async def stream():
            try:
                for k in range(n_out):
                    await asyncio.sleep(TPOT_SLOPE * resident["tokens"] + TPOT_INTERCEPT)
                    resident["tokens"] += 1
                    chunk = {
                        "id": f"cmpl-{iid}",
                        "object": "text_completion.chunk",
                        "created": int(time.time()),
                        "choices": [{"index": 0, "text": f" t{k}", "finish_reason": None}],
                    }
                    if body.get("return_token_ids"):
                        chunk["choices"][0]["token_ids"] = [k]
                    if k == n_out - 1:
                        chunk["choices"][0]["finish_reason"] = "length"
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                resident["tokens"] -= n_in + n_out

        if body.get("stream"):
            return StreamingResponse(stream(), media_type="text/event-stream")
        resident["tokens"] -= n_in
        text = "".join(f" t{k}" for k in range(n_out))
        return JSONResponse(
            {
                "id": f"cmpl-{iid}",
                "object": "text_completion",
                "choices": [{"index": 0, "text": text, "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": n_in,
                    "completion_tokens": n_out,
                    "total_tokens": n_in + n_out,
                },
            }
        )

    return app


def _serve_one(iid: str, model: str, port: int, version: str = STUB_VERSION) -> None:
    uvicorn.run(build(iid, model, version), host="127.0.0.1", port=port, log_level="warning")


def _check_ports_available(base_port: int, instances: int) -> None:
    if base_port < 1 or base_port + instances - 1 > 65535:
        raise SystemExit("stub port range must be within 1-65535")
    for port in range(base_port, base_port + instances):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
                listener.listen(1)
            except OSError as exc:
                raise SystemExit(
                    f"127.0.0.1:{port} is unavailable ({exc}); choose a free stub base port"
                ) from exc


def _write_fleet(path: Path, base_port: int, instances: int, model: str) -> None:
    template = Path(__file__).resolve().parents[1] / "config/fleet.stub.json"
    if path.resolve() == template.resolve():
        raise SystemExit("refusing to overwrite config/fleet.stub.json")
    fleet = json.loads(template.read_text())
    if len(fleet["engines"]) != instances:
        raise SystemExit(
            f"stub fleet template has {len(fleet['engines'])} engines, not {instances}"
        )
    fleet["model"] = model
    for index, engine in enumerate(fleet["engines"]):
        url = f"http://127.0.0.1:{base_port + index}"
        engine["url"] = url
        engine["attestation_url"] = f"{url}/v1/attestation"
    fleet["profiles"]["path"] = str(path.parent / "profiles.json")
    contents = json.dumps(fleet, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x") as output:
            output.write(contents)
    except FileExistsError as exc:
        if path.read_text() != contents:
            raise SystemExit(f"{path} already exists with different contents") from exc
    print(f"stub fleet config: {path}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a stub vLLM fleet for a dry run")
    ap.add_argument("--base-port", type=int, default=8101)
    ap.add_argument("--instances", type=int, default=6)
    ap.add_argument("--model", default="stub")
    ap.add_argument("--version", default=STUB_VERSION)
    ap.add_argument("--write-fleet", type=Path, help="write a matching ignored fleet config")
    ap.add_argument(
        "--single-iid",
        default="",
        help="serve one named engine in the foreground at --base-port",
    )
    ap.add_argument(
        "--mismatch-version-at",
        type=int,
        default=-1,
        metavar="INDEX",
        help="give one zero-based stub a different /version response",
    )
    args = ap.parse_args()

    if args.instances < 1:
        raise SystemExit("--instances must be positive")
    if args.single_iid:
        if args.write_fleet:
            raise SystemExit("--write-fleet requires the full stub fleet")
        _serve_one(args.single_iid, args.model, args.base_port, args.version)
        return 0
    _check_ports_available(args.base_port, args.instances)
    if args.write_fleet:
        _write_fleet(args.write_fleet, args.base_port, args.instances, args.model)

    # Give each stub a process. Six engines on one event loop stretch the
    # modeled 12 ms token interval to 160 ms and invalidate the SLO checks.
    procs = []
    for k in range(args.instances):
        version = f"{args.version}-mismatch" if k == args.mismatch_version_at else args.version
        p = multiprocessing.Process(
            target=_serve_one,
            args=(f"e{k}", args.model, args.base_port + k, version),
            daemon=True,
        )
        p.start()
        procs.append(p)
    last = args.base_port + args.instances - 1
    print(f"stub fleet: {args.instances} instances on 127.0.0.1:{args.base_port}-{last}")
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
