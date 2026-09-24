"""Run a reproducible synthetic token-length workload through the deployment router."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import random
import resource
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from narwhal.engines.stream import event_choices, event_object, token_ids

SEED_PROMPT = (
    "List varied ordinary words about geography, science, cooking, music, sports, "
    "transport, history, and nature. Separate the words with spaces."
)


def private_json(path: Path, value: object) -> None:
    with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance() -> dict:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=True,
    )
    return {
        "host": platform.node(),
        "revision": result.stdout.strip(),
        "helper_sha256": digest(Path(__file__)),
        "python": platform.python_version(),
        "httpx": httpx.__version__,
        "command": sys.argv,
        "started_at": time.time(),
    }


def load_workload(path: Path) -> dict:
    value = json.loads(path.read_text())
    if value.get("schema") != 1 or value.get("kind") != "synthetic-token-length":
        raise ValueError("Workload requires schema 1 and synthetic-token-length kind")
    for key, minimum in (("input_tokens", 1), ("output_tokens", 1), ("seed", 0)):
        if type(value.get(key)) is not int or value[key] < minimum:
            raise ValueError(f"Workload requires integer {key} >= {minimum}")
    pool = value.get("token_pool")
    if not isinstance(pool, list) or not pool or any(type(t) is not int or t < 0 for t in pool):
        raise ValueError("Workload requires a nonempty valid token pool")
    if not isinstance(value.get("model"), str) or not value["model"]:
        raise ValueError("Workload requires a model")
    return value


def body_for(workload: dict, sequence: int) -> dict:
    rng = random.Random(workload["seed"] + sequence)
    return {
        "model": workload["model"],
        "prompt": [rng.choice(workload["token_pool"]) for _ in range(workload["input_tokens"])],
        "max_tokens": workload["output_tokens"],
        "min_tokens": workload["output_tokens"],
        "ignore_eos": True,
        "add_special_tokens": False,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "return_token_ids": True,
        "stream_interval": 1,
    }


async def request_one(client, base, body, rid, scheduled, timeout, clock=time.monotonic):
    started = clock()
    row = {
        "client_rid": rid,
        "scheduled_mono": scheduled,
        "started_at": time.time(),
        "scheduled_at": time.time() - (started - scheduled),
        "schedule_lag_s": max(0.0, started - scheduled),
        "sent": True,
        "status": None,
        "outcome": "invalid_stream",
        "output_tokens": 0,
        "input_tokens": None,
        "ttft_s": None,
        "tpot_s": None,
    }
    first = last = None
    finished = done = False
    usage = None
    try:
        async with asyncio.timeout(timeout):
            async with client.stream(
                "POST", base + "/v1/completions", json=body, headers={"x-request-id": rid}
            ) as response:
                row["status"] = response.status_code
                if response.status_code != 200:
                    row["outcome"] = "http_error"
                    row["error_body"] = (await response.aread()).decode(errors="replace")[:4096]
                    return row
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if line[5:].strip() == "[DONE]":
                        done = True
                        break
                    obj = event_object(line)
                    if obj is None:
                        continue
                    if obj.get("error"):
                        row["error_body"] = str(obj["error"])[:4096]
                        raise ValueError("stream_error")
                    choices = event_choices(obj)
                    if len(choices) > 1 or any(c.get("index", 0) != 0 for c in choices):
                        raise ValueError("multiple_choices")
                    ids = token_ids(choices)
                    if ids is None or len(ids) > 1:
                        raise ValueError("requires_one_identified_token_per_event")
                    if ids:
                        if finished:
                            raise ValueError("tokens_after_finish")
                        last = clock()
                        if first is None:
                            first = last
                        row["output_tokens"] += 1
                        if row["output_tokens"] > body["max_tokens"]:
                            raise ValueError("output_exceeds_requested_length")
                    for choice in choices:
                        if choice.get("finish_reason") is not None:
                            if finished or choice["finish_reason"] != "length":
                                raise ValueError("invalid_finish_reason")
                            finished = True
                    if obj.get("usage") is not None:
                        usage = obj["usage"]
                if isinstance(usage, dict):
                    row["input_tokens"] = usage.get("prompt_tokens")
                if not (
                    done
                    and finished
                    and row["output_tokens"] == body["max_tokens"]
                    and isinstance(usage, dict)
                    and type(usage.get("prompt_tokens")) is int
                    and usage["prompt_tokens"] == len(body["prompt"])
                    and type(usage.get("completion_tokens")) is int
                    and usage["completion_tokens"] == row["output_tokens"]
                ):
                    raise ValueError("incomplete_stream_or_token_usage_mismatch")
                row["outcome"] = "completed"
    except (httpx.HTTPError, TimeoutError, ValueError) as error:
        row["error"] = str(error)[:4096]
        if isinstance(error, TimeoutError):
            row["outcome"] = "timeout"
        elif isinstance(error, httpx.HTTPError):
            row["outcome"] = "transport_error"
    finally:
        row["elapsed_s"] = clock() - started
        if first is not None:
            row["ttft_s"] = first - started
        if last is not None and first is not None and row["output_tokens"] >= 2:
            row["tpot_s"] = (last - first) / (row["output_tokens"] - 1)
    return row


def idle(state: dict) -> bool:
    admission = state["admission"]
    return (
        all(
            admission[key] == 0
            for key in ("inflight", "queued", "waiting_prefill", "waiting_decode")
        )
        and state["serving"]["http_retained"] == 0
        and all(r["prefill"] == 0 and r["decode"] == 0 for r in state["resident"].values())
    )


async def poll_drain(client, base, timeout, poll_s=1):
    deadline = time.monotonic() + timeout
    result = {"condition": "timeout", "polls": 0}
    while True:
        try:
            response = await client.get(
                base + "/narwhal/state",
                timeout=min(10.0, max(0.1, deadline - time.monotonic())),
            )
            response.raise_for_status()
            state = response.json()
            result["polls"] += 1
            result["last_state"] = state
            if idle(state):
                result["condition"] = "idle"
                break
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            result["condition"] = "state_error"
            result["error"] = str(error)
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
    return result


async def drain(client, base, timeout):
    result = await poll_drain(client, base, timeout)
    if result["condition"] == "idle":
        return result["last_state"]
    if result["condition"] == "state_error":
        raise ValueError(f"Router drain state error: {result['error']}")
    raise ValueError("Router drain deadline: inspect admission and resident work")


def summary(rows, args, elapsed):
    valid = len(rows) == args.requests and all(
        r["sent"] and r["schedule_lag_s"] <= args.max_lag for r in rows
    )
    completed = [r for r in rows if r["outcome"] == "completed"]
    passed = [
        r
        for r in completed
        if r["ttft_s"] <= args.ttft
        and (r["output_tokens"] == 1 or (r["tpot_s"] is not None and r["tpot_s"] <= args.tpot))
    ]

    def distribution(key):
        values = sorted(r[key] for r in completed if r[key] is not None)
        return {
            f"p{p}": values[max(0, math.ceil(len(values) * p / 100) - 1)] if values else None
            for p in (50, 95, 99)
        }

    return {
        "offered": args.requests,
        "sent": sum(r["sent"] for r in rows),
        "terminal": len(rows),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        "completed": len(completed),
        "within_candidate_limits": len(passed),
        "attainment": len(passed) / args.requests,
        "required_attainment": args.attainment,
        "client_schedule_valid": valid,
        "candidate_pass": valid and len(passed) / args.requests >= args.attainment,
        "offered_rate_rps": args.rate,
        "offer_window_s": args.requests / args.rate,
        "measurement_window_including_drain_s": elapsed,
        "completed_rps_including_drain": len(completed) / elapsed,
        "completed_output_tokens_per_s_including_drain": sum(r["output_tokens"] for r in completed)
        / elapsed,
        "qualified_rps_including_drain": len(passed) / elapsed,
        "ttft_s": distribution("ttft_s"),
        "tpot_s": distribution("tpot_s"),
        "max_schedule_lag_s": max(r["schedule_lag_s"] for r in rows),
    }


async def prepare(client, base, args):
    response = await client.get(base + "/v1/models")
    response.raise_for_status()
    models = response.json()["data"]
    if len(models) != 1:
        raise ValueError("Workload preparation requires the router's single served model")
    model = models[0]["id"]
    response = await client.post(
        base + "/v1/completions",
        json={
            "model": model,
            "prompt": SEED_PROMPT,
            "max_tokens": 32,
            "min_tokens": 32,
            "ignore_eos": True,
            "temperature": 0.0,
            "return_token_ids": True,
            "stream": False,
        },
    )
    response.raise_for_status()
    seed = response.json()
    choices = event_choices(seed)
    generated_ids = token_ids(choices)
    if generated_ids is None or len(generated_ids) != 32 or seed.get("error"):
        raise ValueError("Seed completion must return 32 identified output tokens")
    prompt_ids = choices[0].get("prompt_token_ids") if len(choices) == 1 else None
    if (
        isinstance(prompt_ids, list)
        and prompt_ids
        and all(type(value) is int and value >= 0 for value in prompt_ids)
        and len(set(prompt_ids)) > 1
    ):
        pool = prompt_ids
    else:
        pool = list(generated_ids)
    if len(set(pool)) < 2:
        raise ValueError("Seed response has no diverse token IDs for the workload")
    private_json(args.out / "seed-response.json", seed)
    private_json(
        args.out / "workload.json",
        {
            "schema": 1,
            "kind": "synthetic-token-length",
            "model": model,
            "input_tokens": args.input_tokens,
            "output_tokens": args.output_tokens,
            "seed": args.seed,
            "token_pool": list(pool),
            "seed_prompt": SEED_PROMPT,
            "recipe": "Python random.Random(seed + sequence).choice(token_pool) per input token",
        },
    )


async def run_trial(client, base, args):
    run_id = getattr(args, "run_id", uuid.uuid4().hex)
    workload = load_workload(args.workload)
    if getattr(args, "expected_model", None) and workload["model"] != args.expected_model:
        raise ValueError("Workload model differs from --expected-model")
    private_json(args.out / "workload.json", workload)
    private_json(args.out / "state-before.json", await drain(client, base, args.timeout))
    warmup = await request_one(
        client,
        base,
        body_for(workload, args.requests),
        f"{run_id}-warmup",
        time.monotonic(),
        args.timeout,
    )
    private_json(args.out / "warmup.json", warmup)
    if warmup["outcome"] != "completed":
        raise ValueError("Warmup failed; inspect warmup.json before offering trial traffic")
    private_json(args.out / "state-after-warmup.json", await drain(client, base, args.timeout))
    private_json(
        args.out / "network-before.json", {"proc_net_dev": Path("/proc/net/dev").read_text()}
    )
    start, cpu = time.monotonic(), time.process_time()
    rows, pending, tasks = [], set(), []
    with open(
        args.out / "requests.jsonl", "x", opener=lambda p, flags: os.open(p, flags, 0o600)
    ) as output:

        def record(row):
            rows.append(row)
            output.write(json.dumps(row, allow_nan=False) + "\n")
            output.flush()

        async def send(sequence, scheduled):
            row = await request_one(
                client,
                base,
                body_for(workload, sequence),
                f"{run_id}-{sequence}",
                scheduled,
                args.timeout,
            )
            row["sequence"] = sequence
            record(row)

        for sequence in range(args.requests):
            scheduled = start + sequence / args.rate
            await asyncio.sleep(max(0.0, scheduled - time.monotonic()))
            lag = max(0.0, time.monotonic() - scheduled)
            if lag > args.max_lag or len(pending) >= args.max_inflight:
                record(
                    {
                        "sequence": sequence,
                        "client_rid": f"{run_id}-{sequence}",
                        "scheduled_mono": scheduled,
                        "scheduled_at": time.time() - lag,
                        "schedule_lag_s": lag,
                        "sent": False,
                        "outcome": "client_schedule_miss",
                        "status": None,
                        "output_tokens": 0,
                        "ttft_s": None,
                        "tpot_s": None,
                    }
                )
                continue
            task = asyncio.create_task(send(sequence, scheduled))
            tasks.append(task)
            pending.add(task)
            task.add_done_callback(pending.discard)
        await asyncio.gather(*tasks)
        await asyncio.sleep(max(0.0, start + args.requests / args.rate - time.monotonic()))
    private_json(args.out / "state-after.json", await drain(client, base, args.timeout))
    private_json(
        args.out / "network-after.json", {"proc_net_dev": Path("/proc/net/dev").read_text()}
    )
    report = summary(rows, args, time.monotonic() - start)
    report["client_cpu_s"] = time.process_time() - cpu
    report["client_max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    private_json(args.out / "summary.json", report)
    print(json.dumps(report, indent=2))
    return 0 if report["candidate_pass"] else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True, type=Path, help="fresh private output directory")
    parser.add_argument(
        "--api-key-env", help="environment variable containing ingress bearer token"
    )
    parser.add_argument("--workload", type=Path)
    parser.add_argument("--expected-model", help="require the workload to name this served model")
    parser.add_argument("--input-tokens", type=int, default=8192)
    parser.add_argument("--output-tokens", type=int, default=128)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--rate", type=float, default=0.5)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--ttft", type=float, default=2.0)
    parser.add_argument("--tpot", type=float, default=0.0333)
    parser.add_argument("--attainment", type=float, default=0.95)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-inflight", type=int, default=64)
    parser.add_argument("--max-lag", type=float, default=0.05)
    args = parser.parse_args(argv)
    for key in (
        "input_tokens",
        "rate",
        "requests",
        "ttft",
        "tpot",
        "timeout",
        "max_inflight",
        "max_lag",
    ):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error(f"--{key.replace('_', '-')} must be finite and positive")
    if not 0 < args.attainment <= 1 or args.output_tokens < 1 or args.seed < 0:
        parser.error("Require attainment in (0, 1], output tokens >= 1, and seed >= 0")
    if args.command == "run" and args.workload is None:
        parser.error("run requires --workload")
    parsed = urlsplit(args.base)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error(
            "--base requires an HTTP origin/path with credentials supplied through --api-key-env"
        )
    headers = {}
    if args.api_key_env:
        if not os.environ.get(args.api_key_env):
            parser.error("The named API-key environment variable must be populated")
        headers["authorization"] = "Bearer " + os.environ[args.api_key_env]
    args.out = args.out.resolve()
    args.run_id = uuid.uuid4().hex

    async def execute():
        async with httpx.AsyncClient(
            headers=headers,
            timeout=args.timeout,
            trust_env=False,
            limits=httpx.Limits(max_connections=args.max_inflight + 1),
        ) as client:
            if args.command == "prepare":
                await prepare(client, args.base.rstrip("/"), args)
                return 0
            return await run_trial(client, args.base.rstrip("/"), args)

    try:
        args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
        private_json(
            args.out / "manifest.json",
            {
                **provenance(),
                "run_id": args.run_id,
                "settings": vars(args) | {"out": str(args.out), "workload": str(args.workload)},
                "workload_sha256": digest(args.workload) if args.workload else None,
            },
        )
        return asyncio.run(execute())
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as error:
        print(f"Load trial blocked: {error}. Retain {args.out} before recovery.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"Trial interrupted; retain partial records in {args.out}.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
