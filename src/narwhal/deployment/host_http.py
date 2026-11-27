"""HTTP checks that `narwhal deploy run` runs on a host, from the installed wheel.

`wait` waits for a route to answer HTTP 200. `responses` retains the router's health,
readiness, state and metrics, and optionally one completion. `readmit` readmits engines to a
running router and waits until each is active. Each prints the files it returns as
`narwhal-file <name> <base64>` lines, and a failure's cause on standard error after
`narwhal-host: `.
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


def _states(base: str) -> tuple[dict[str, Any], dict[str, str]]:
    status, content = _request(base + "/narwhal/lifecycle")
    if status != 200:
        raise OSError(f"GET /narwhal/lifecycle answered HTTP {status}")
    lifecycle = json.loads(content)
    return lifecycle, {
        iid: str(engine.get("state")) for iid, engine in lifecycle.get("engines", {}).items()
    }


def readmit(base: str, engines: list[str], seconds: float) -> int:
    """Readmit each engine that is not active, then wait until every engine is active."""
    before, states = _states(base)
    waiting = [iid for iid in engines if states.get(iid) != "active"]
    result: dict[str, Any] = {"engines": engines, "lifecycle_before": before, "readmitted": None}
    if waiting:
        status, content = _request(
            base + "/narwhal/lifecycle/readmit", json.dumps({"engines": waiting}).encode()
        )
        result["readmitted"] = {"status": status, "body": json.loads(content or b"null")}
    deadline = time.monotonic() + seconds
    while True:
        after, states = _states(base)
        pending = [iid for iid in engines if states.get(iid) != "active"]
        if not pending or time.monotonic() > deadline:
            break
        time.sleep(1)
    result["lifecycle_after"] = after
    _emit("readmission.json", json.dumps(result, indent=2).encode())
    if pending:
        return _fail(
            f"engines {', '.join(pending)} are not active after readmission; read "
            "engines.<id>.error and checks in GET /narwhal/lifecycle"
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
    readmitted.add_argument("engines", nargs="*")
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
