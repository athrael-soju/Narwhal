"""HTTP checks that `narwhal deploy run` runs on a host, from the installed wheel.

`wait` waits for a route to answer HTTP 200. `responses` retains the router's health,
readiness, state, metrics and lifecycle, and optionally one completion. `readmit` waits until
a running router serves each restarted engine's new process, readmitting a held engine.
Each prints the files it returns as `narwhal-file <name> <base64>` lines, and a failure's
cause on standard error after `narwhal-host: `.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

CAUSE = "narwhal-host: "
ROUTES = (
    ("health.json", "/health"),
    ("ready.json", "/ready"),
    ("state.json", "/narwhal/state"),
    ("metrics.txt", "/metrics"),
    ("lifecycle.json", "/narwhal/lifecycle"),
)


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


def responses(base: str, completion: str | None) -> int:
    routes: list[tuple[str, str, bytes | None]] = [(n, p, None) for n, p in ROUTES]
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
    readmitted = commands.add_parser("readmit")
    readmitted.add_argument("base")
    readmitted.add_argument("seconds", type=float)
    readmitted.add_argument("engines", nargs="*", metavar="IID[=START]")
    args = parser.parse_args(argv)
    try:
        if args.command == "wait":
            return wait(args.url, args.seconds)
        if args.command == "responses":
            return responses(args.base.rstrip("/"), args.completion)
        return readmit(args.base.rstrip("/"), args.engines, args.seconds)
    except (OSError, ValueError) as error:
        return _fail(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
