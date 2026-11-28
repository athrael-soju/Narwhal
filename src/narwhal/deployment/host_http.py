"""HTTP checks that `narwhal deploy run` runs on a host, from the installed wheel.

`wait` waits for a route to answer HTTP 200, and `status` prints the HTTP status a route
answers. `responses` retains the router's health, readiness, state, metrics and lifecycle,
and optionally one completion. `readmit` waits until a running router serves each restarted
engine's new process, readmitting a held engine. `overhead` measures the time the router adds
before the first token while it serves concurrent completions.
Each prints the files it returns as `narwhal-file <name> <base64>` lines, and a failure's
cause on standard error after `narwhal-host: `.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

CAUSE = "narwhal-host: "
ROUTES = (
    ("health.json", "/health"),
    ("ready.json", "/ready"),
    ("state.json", "/narwhal/state"),
    ("metrics.txt", "/metrics"),
    ("lifecycle.json", "/narwhal/lifecycle"),
)
# What the load balancer forwards of `ROUTES`; it answers 404 on every other route.
SERVING = ("/health", "/ready")


def _fail(message: str) -> int:
    print(CAUSE + message, file=sys.stderr)
    return 1


def _emit(name: str, content: bytes) -> None:
    print(f"narwhal-file {name} {base64.b64encode(content).decode()}")


def _request(url: str, data: bytes | None = None, timeout: float = 30) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def wait(url: str, seconds: float) -> int:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if _request(url, timeout=2)[0] == 200:
                return 0
        except OSError:
            pass
        time.sleep(0.5)
    return _fail(f"{url} did not answer HTTP 200 within {seconds:g} seconds")


def status(url: str) -> int:
    """Print the HTTP status `url` answers, or 0 when nothing answers."""
    try:
        code = _request(url, timeout=5)[0]
    except OSError:
        code = 0
    print(code)
    return 0


_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})? (\S+)$")


def _metrics(base: str) -> dict[tuple[str, str], float]:
    """The router's samples by name and label set."""
    code, content = _request(base + "/metrics")
    if code != 200:
        raise OSError(f"GET /metrics answered HTTP {code}")
    samples = {}
    for line in content.decode().splitlines():
        if match := _SAMPLE.match(line):
            samples[(match[1], match[2] or "")] = float(match[3])
    return samples


def _total(samples: dict[tuple[str, str], float], name: str, *labels: str) -> float:
    """The sum of `name`'s samples whose label set holds every one of `labels`."""
    return sum(
        value
        for (sample, series), value in samples.items()
        if sample == name and all(label in series for label in labels)
    )


def overhead_terms(
    before: dict[tuple[str, str], float], after: dict[tuple[str, str], float]
) -> dict[str, float]:
    """The router's own first-token time over a window, from two scrapes of its `/metrics`.

    Over the requests that reached their first token in the window, the router overhead is
    the router's time to first token less the prefill legs' upstream time and the waits for
    an admission seat and a prefill seat, divided by the number of those requests.
    """

    def delta(name: str, *labels: str) -> float:
        return _total(after, name, *labels) - _total(before, name, *labels)

    terms = {
        "requests": delta("narwhal_ttft_seconds_count"),
        "ttft_seconds": delta("narwhal_ttft_seconds_sum"),
        "prefill_upstream_seconds": delta("narwhal_upstream_seconds_total", 'phase="prefill"'),
        "admission_wait_seconds": delta("narwhal_queue_wait_seconds_sum", 'stage="admission"'),
        "prefill_wait_seconds": delta("narwhal_queue_wait_seconds_sum", 'stage="prefill"'),
    }
    own = (
        terms["ttft_seconds"]
        - terms["prefill_upstream_seconds"]
        - terms["admission_wait_seconds"]
        - terms["prefill_wait_seconds"]
    )
    reset = sorted(name for name, value in terms.items() if value < 0)
    if reset:
        raise CounterReset(f"the router's counters went back over the window: {', '.join(reset)}")
    terms["overhead_seconds"] = own / terms["requests"] if terms["requests"] else 0.0
    return terms


class CounterReset(ValueError):
    """The router's counters restarted between the two scrapes of a measurement window."""


# Windows the overhead check measures before it gives up on a router whose counters reset.
OVERHEAD_ATTEMPTS = 3


def overhead(base: str, completion: str, requests: int, concurrency: int, limit: float) -> int:
    """Send `requests` completions at `concurrency` and compare the router overhead to `limit`.

    A window in which the router's counters reset, or count fewer first tokens than the
    completions that succeeded, measured a restarted router; it is measured again.
    """
    body = completion.encode()

    def send(_: int) -> int:
        try:
            return _request(base + "/v1/completions", body, timeout=300)[0]
        except OSError:
            return 0

    resets = []
    for _ in range(OVERHEAD_ATTEMPTS):
        before = _metrics(base)
        with ThreadPoolExecutor(concurrency) as pool:
            statuses = list(pool.map(send, range(requests)))
        after = _metrics(base)
        counts: dict[str, int] = {}
        for code in statuses:
            counts[str(code)] = counts.get(str(code), 0) + 1
        try:
            terms = overhead_terms(before, after)
        except CounterReset as reset:
            resets.append(str(reset))
            continue
        if terms["requests"] < counts.get("200", 0):
            resets.append(
                f"the router counted {terms['requests']:g} first tokens for "
                f"{counts.get('200', 0)} completed requests"
            )
            continue
        break
    else:
        result = {"requests_sent": requests, "resets": resets, "limit_seconds": limit}
        result["problem"] = (
            f"the router's counters reset in each of {OVERHEAD_ATTEMPTS} measurement windows: "
            + "; ".join(resets)
        )
        _emit("overhead.json", json.dumps(result, indent=2).encode())
        return 0
    failed = requests - counts.get("200", 0)
    problem = None
    if failed:
        problem = f"{failed} of {requests} measurement requests failed: {counts}"
    elif not terms["requests"]:
        problem = "the router's time to first token counted no measurement request"
    elif terms["overhead_seconds"] > limit:
        problem = (
            f"router overhead {terms['overhead_seconds']:.4f}s over {terms['requests']:g} "
            f"requests at concurrency {concurrency} exceeds NARWHAL_ROUTER_OVERHEAD_S {limit:g}s"
        )
    result = {
        "requests_sent": requests,
        "concurrency": concurrency,
        "statuses": counts,
        **terms,
        "limit_seconds": limit,
        "resets": resets,
        "problem": problem,
    }
    # The verdict is in the file; the command fails only when it cannot measure.
    _emit("overhead.json", json.dumps(result, indent=2).encode())
    return 0


def responses(base: str, completion: str | None, *, serving: bool = False) -> int:
    """Keep the router's answers at `base`; with `serving`, only the routes a load balancer
    forwards."""
    routes: list[tuple[str, str, bytes | None]] = [
        (n, p, None) for n, p in ROUTES if not serving or p in SERVING
    ]
    if completion:
        routes.append(("completion.json", "/v1/completions", completion.encode()))
    failed = []
    for name, path, data in routes:
        status, content = _request(base + path, data, timeout=120)
        _emit(name, content)
        if status != 200:
            failed.append(f"{path} answered HTTP {status}")
    return _fail("; ".join(failed)) if failed else 0


def _lifecycle(base: str) -> dict[str, Any]:
    status, content = _request(base + "/narwhal/lifecycle")
    if status != 200:
        raise OSError(f"GET /narwhal/lifecycle answered HTTP {status}")
    document: dict[str, Any] = json.loads(content)
    return document


def _reason(document: Any, iid: str) -> str:
    """The router's own cause for refusing `iid`, from a lifecycle document."""
    if not isinstance(document, dict):
        return "the router gave no reason"
    engine = document.get("engines", {}).get(iid, {})
    parts = [document.get("error") or "", engine.get("error") or ""]
    checks = engine.get("checks") or []
    if checks:
        parts.append("passed checks: " + ", ".join(checks))
    return "; ".join(part for part in parts if part) or "the router gave no reason"


def readmit(base: str, engines: list[str], seconds: float) -> int:
    """Wait until the router serves each restarted engine's new process, readmitting it.

    Each of `engines` is `IID=START`, START being the process start the router had accepted
    for the engine before its restart, or a bare `IID` when none was retained. The router
    finds a restarted process itself: its liveness sweep ejects the engine for
    `process_identity`, and recovery returns it to placement when its checks pass. An
    engine the router holds `blocked` or `drained` is readmitted with one request of its own,
    and a refused readmission fails with the router's reason. An engine is back once it is
    `active`, accepts new requests and the router's accepted process start differs from START.
    """
    baseline: dict[str, float | None] = {}
    for item in engines:
        iid, _, start = item.partition("=")
        baseline[iid] = float(start) if start else None
    result: dict[str, Any] = {"baseline": baseline, "requests": []}
    pending = list(baseline)
    waiting: dict[str, str] = {}
    deadline = time.monotonic() + seconds
    while True:
        lifecycle = _lifecycle(base)
        result.setdefault("lifecycle_before", lifecycle)
        result["lifecycle_after"] = lifecycle
        if lifecycle.get("wave", {}).get("active"):
            _emit("readmission.json", json.dumps(result, indent=2).encode())
            return _fail(
                "the router holds a whole-wave restart; restart the engine wave as "
                "docs/operate/03-Restart-Engines.md describes"
            )
        starts = lifecycle.get("process_starts", {})
        for iid in list(pending):
            engine = lifecycle.get("engines", {}).get(iid)
            if engine is None:
                _emit("readmission.json", json.dumps(result, indent=2).encode())
                return _fail(f"the router's fleet has no engine {iid}")
            state = engine.get("state")
            if state in ("blocked", "drained"):
                status, content = _request(
                    base + "/narwhal/lifecycle/readmit", json.dumps({"engines": [iid]}).encode()
                )
                body = json.loads(content or b"null")
                result["requests"].append({"engine": iid, "status": status, "body": body})
                if status != 200:
                    _emit("readmission.json", json.dumps(result, indent=2).encode())
                    return _fail(
                        f"the router refused to readmit {iid} with HTTP {status}: "
                        f"{_reason(body, iid)}"
                    )
                continue
            start = starts.get(iid)
            replaced = baseline[iid] is None or (start is not None and start != baseline[iid])
            if state == "active" and engine.get("accepts_new") and replaced:
                pending.remove(iid)
                waiting.pop(iid, None)
            elif not replaced:
                waiting[iid] = "the router has not yet seen its restarted process"
            else:
                waiting[iid] = (
                    f"{state}, {'accepting' if engine.get('accepts_new') else 'out of placement'}"
                )
                if engine.get("error"):
                    waiting[iid] += f": {engine['error']}"
        if not pending or time.monotonic() > deadline:
            break
        time.sleep(1)
    _emit("readmission.json", json.dumps(result, indent=2).encode())
    if pending:
        details = "; ".join(f"{iid}: {waiting.get(iid, 'pending')}" for iid in pending)
        return _fail(
            f"engines {', '.join(pending)} are not back in placement after {seconds:g} "
            f"seconds ({details}); read engines.<id> in GET /narwhal/lifecycle"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    waiting = commands.add_parser("wait")
    waiting.add_argument("url")
    waiting.add_argument("seconds", type=float)
    kept = commands.add_parser("responses")
    kept.add_argument("base")
    kept.add_argument("--completion")
    kept.add_argument("--serving", action="store_true")
    answered = commands.add_parser("status")
    answered.add_argument("url")
    measured = commands.add_parser("overhead")
    measured.add_argument("base")
    measured.add_argument("--completion", required=True)
    measured.add_argument("--requests", type=int, required=True)
    measured.add_argument("--concurrency", type=int, required=True)
    measured.add_argument("--limit", type=float, required=True)
    readmitted = commands.add_parser("readmit")
    readmitted.add_argument("base")
    readmitted.add_argument("seconds", type=float)
    readmitted.add_argument("engines", nargs="*", metavar="IID[=START]")
    args = parser.parse_args(argv)
    try:
        if args.command == "wait":
            return wait(args.url, args.seconds)
        if args.command == "status":
            return status(args.url)
        if args.command == "overhead":
            return overhead(
                args.base.rstrip("/"), args.completion, args.requests, args.concurrency, args.limit
            )
        if args.command == "responses":
            return responses(args.base.rstrip("/"), args.completion, serving=args.serving)
        return readmit(args.base.rstrip("/"), args.engines, args.seconds)
    except (OSError, ValueError) as error:
        return _fail(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
