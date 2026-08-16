#!/usr/bin/env python3
"""A fake vLLM fleet: the disaggregated protocol, with the Arrow paper's timing.

This exists so the deployment path can be exercised end to end without an
accelerator. It answers `/health`, `/tokenize` and `/v1/completions`, honours
`kv_transfer_params`, and streams. Timing follows Arrow §3.1: prefill quadratic in
input length, decode linear in the tokens resident on that instance.

What it validates is the wiring: profiling, admission, both legs, the handoff,
the token stream, flipping, the journal. What it cannot validate is anything
about a real accelerator, so a green run here is a precondition for a fleet
run and never a substitute for one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# The same coefficients the simulator uses, so a stub run and a simulated run
# are comparable.
TTFT_A = 2e-8
TTFT_B = 6e-5
TTFT_C = 0.005
TPOT_SLOPE = 3e-6
TPOT_INTERCEPT = 0.012
CHARS_PER_TOKEN = 3.8


def build(iid: str, model: str) -> FastAPI:
    app = FastAPI(title=f"stub-{iid}")
    resident = {"tokens": 0}

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
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                resident["tokens"] -= n_in + n_out

        if body.get("stream"):
            return StreamingResponse(stream(), media_type="text/event-stream")
        text = "".join(f" t{k}" for k in range(n_out))
        return JSONResponse(
            {
                "id": f"cmpl-{iid}",
                "object": "text_completion",
                "choices": [{"index": 0, "text": text, "finish_reason": "length"}],
            }
        )

    return app


def _serve_one(iid: str, model: str, port: int) -> None:
    uvicorn.run(build(iid, model), host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a stub vLLM fleet for a dry run")
    ap.add_argument("--base-port", type=int, default=8101)
    ap.add_argument("--instances", type=int, default=6)
    ap.add_argument("--model", default="stub")
    args = ap.parse_args()

    # One process per instance. Sharing an event loop across the fleet made the
    # stub, not the scheduler, the bottleneck: six engines on one loop measured
    # a 160 ms token interval against the 12 ms the timing model asks for, so
    # every SLO gate failed against a limit that belonged to the harness.
    procs = []
    for k in range(args.instances):
        p = multiprocessing.Process(
            target=_serve_one, args=(f"e{k}", args.model, args.base_port + k), daemon=True
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
