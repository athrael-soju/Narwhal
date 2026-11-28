"""The capacity trial of `narwhal deploy run --trial`: its definition, its prerequisites and the
Prometheus series it checks.

The trial definition is a `narwhal.capacity-trial` document: the synthetic workload, the
offered rates and the thresholds each rate must meet. The series checks are data, so
`docs/deploy/07-Serve-and-Measure.md` lists them and promtool tests evaluate them. Every query
goes through a `Query`, a callable that returns Prometheus's instant vector for an expression,
so tests replace Prometheus with fixtures.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from narwhal.backends import load as load_backend
from narwhal.backends import names as backend_names

from ..contracts import TRIAL, ContractVersionError, validate_document
from ..diagnostics.bundle import DIAGNOSTIC_HEADERS
from ..engines.host_process import started_at_seconds
from .launch_engine.runtime import regular_file_digest

# The optional fields of a definition and their defaults, the load client's own.
DEFAULTS = {"max_inflight": 64, "max_lag_s": 0.05, "timeout_s": 120.0}
_REQUIRED = (
    "input_tokens",
    "output_tokens",
    "seed",
    "requests",
    "rates_rps",
    "ttft_s",
    "tpot_s",
    "attainment",
)


@dataclass(frozen=True)
class Definition:
    """One capacity trial: the workload, its offered rates in order, and each rate's limits."""

    input_tokens: int
    output_tokens: int
    seed: int
    requests: int
    rates_rps: tuple[float, ...]
    ttft_s: float
    tpot_s: float
    attainment: float
    max_inflight: int = 64
    max_lag_s: float = 0.05
    timeout_s: float = 120.0


def _integer(value: Any, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _positive(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def problems(document: Any) -> list[str]:
    """Every problem of a trial definition, each naming its field; none for a valid one."""
    try:
        validate_document(document, TRIAL)
    except ContractVersionError as error:
        return [str(error)]
    found = []
    allowed = {"schema", "schema_version", *_REQUIRED, *DEFAULTS}
    if unknown := sorted(set(document) - allowed):
        found.append(f"unknown fields {', '.join(unknown)}")
    found += [f"set {name}" for name in _REQUIRED if name not in document]
    for name, minimum in (
        ("input_tokens", 1),
        ("output_tokens", 1),
        ("seed", 0),
        ("requests", 1),
        ("max_inflight", 1),
    ):
        if name in document and not _integer(document[name], minimum):
            found.append(f"set {name} to an integer of at least {minimum}")
    for name in ("ttft_s", "tpot_s", "max_lag_s", "timeout_s"):
        if name in document and not _positive(document[name]):
            found.append(f"set {name} to a positive number of seconds")
    attainment = document.get("attainment")
    if "attainment" in document and not (_positive(attainment) and attainment <= 1):
        found.append("set attainment to a fraction above 0 and at most 1")
    rates = document.get("rates_rps")
    if "rates_rps" in document and not (
        isinstance(rates, list)
        and rates
        and all(_positive(rate) for rate in rates)
        and all(a < b for a, b in pairwise(rates))
    ):
        found.append("set rates_rps to a list of positive request rates in increasing order")
    return found


@dataclass(frozen=True)
class Snapshot:
    """The bytes of a `--trial` file, read once. The run's trial input is their digest and the
    trial stage runs from them, so both describe the same file even when it changes on disk."""

    path: Path
    data: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def snapshot(path: Path) -> Snapshot:
    """Read the regular file at `path`, without following a symbolic link in its last component."""
    try:
        regular_file_digest(path)
        return Snapshot(path, path.read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError(f"--trial {path}: {error}") from None


def load(path: Path | Snapshot) -> Definition:
    """Read and check the trial definition at `path`, or in a snapshot of it; a ValueError names
    every problem."""
    taken = path if isinstance(path, Snapshot) else snapshot(path)
    try:
        document = json.loads(taken.data)
    except ValueError as error:
        raise ValueError(f"--trial {taken.path}: {error}") from None
    if found := problems(document):
        raise ValueError(f"--trial {taken.path}: " + "; ".join(found))
    fields = {**DEFAULTS, **{k: v for k, v in document.items() if not k.startswith("schema")}}
    fields["rates_rps"] = tuple(fields["rates_rps"])
    return Definition(**fields)


def rate_name(rate: float) -> str:
    """The directory name of one offered rate, such as `rate-0.5`."""
    return f"rate-{rate:g}"


# Prerequisites the stage checks before it offers any load.


def caching_problems(checked: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The engines whose `checked.json` does not show prefix caching off."""
    found = []
    for role, record in sorted(checked.items()):
        value = record.get("prefix_caching")
        if value is not False:
            shown = "no prefix_caching" if value is None else f"prefix_caching {json.dumps(value)}"
            found.append(
                f"{role}: checked.json shows {shown}; the capacity trial needs prefix caching "
                "off, so set NARWHAL_PREFIX_CACHING=false before discovery and run again"
            )
    return found


def domain_problems(definition: Definition, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Where the workload leaves the measured domain of an engine's profile.

    A prompt must lie in the measured prompt-length range and an output in the measured
    output-length range, where the profile records them, and one request's KV tokens must fit
    the measured decode range.
    """
    if not rows:
        return ["the profile store holds no profiles"]
    found = []
    tokens = definition.input_tokens + definition.output_tokens
    for row in rows:
        iid = row.get("iid", "?")
        for low_field, high_field, value, what in (
            ("prefill_min_tokens", "prefill_max_tokens", definition.input_tokens, "input_tokens"),
            (
                "decode_min_output_tokens",
                "decode_max_output_tokens",
                definition.output_tokens,
                "output_tokens",
            ),
        ):
            low, high = row.get(low_field), row.get(high_field)
            if isinstance(low, int) and isinstance(high, int) and not low <= value <= high:
                found.append(f"{iid}: {what} {value} is outside the profiled {low} to {high}")
        kv = row.get("decode_max_kv_tokens")
        if isinstance(kv, int) and tokens > kv:
            found.append(
                f"{iid}: one request's {tokens} tokens exceed the profiled {kv} resident KV tokens"
            )
    return found


# The trial reaches the load balancer, the routers and Prometheus through SSH forwards that
# listen on Unix sockets in a private directory, so no other workstation user can take their
# place. The origin names no host; the socket does.
LOCAL_ORIGIN = "http://localhost"


def unix_client(socket: Path, timeout: float) -> httpx.Client:
    """An HTTP client whose every request goes through the Unix socket at `socket`."""
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=str(socket)), timeout=timeout, trust_env=False
    )


# Router samples, as the benchmark evidence collector takes them, for the reconciliation.


def router_sample(client: httpx.Client, origin: str) -> dict[str, Any]:
    """One router's state, metrics and state again; the second state read vouches that the
    metrics belong to the same journal run. The state reads are diagnostic, so a standby
    answers them with its own state instead of refusing."""
    item: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "monotonic": time.monotonic(),
        "router": origin,
        "errors": {},
    }
    for name, path, decoded in (
        ("state", "/narwhal/state", True),
        ("router_metrics", "/metrics", False),
        ("state_after", "/narwhal/state", True),
    ):
        try:
            response = client.get(origin + path, headers=DIAGNOSTIC_HEADERS if decoded else None)
            response.raise_for_status()
            item[name] = response.json() if decoded else response.text
        except (httpx.HTTPError, ValueError) as error:
            item["errors"][name] = str(error)
    if "state_after" in item:
        after = item.pop("state_after")
        item["journal_run_after"] = after.get("journal_run")
        item["ha_after"] = after.get("ha") or {}
    elif "state_after" in item["errors"]:
        item["journal_run_after"] = None
    return item


def sample_router(socket: Path, router: str) -> dict[str, Any]:
    """`router_sample` of the router forwarded to `socket`, recorded under its name."""
    with unix_client(socket, 10) as client:
        return {**router_sample(client, LOCAL_ORIGIN), "router": router}


# The router that controls the fleet, read from the samples taken before and after a rate.


def control(samples: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """The router that held the lease across one round of samples, with its epoch and holder
    token, or None and the reason no single router did.

    A router reports `ha.role` `active` in both of its state reads; a router without `ha`, such
    as a single router without fencing, is active at epoch 0.
    """
    held = []
    for sample in samples:
        name = str(sample.get("router"))
        if "state" not in sample or "ha_after" not in sample:
            return None, f"{name}'s state could not be read"
        first = sample["state"].get("ha") or {}
        second = sample["ha_after"]
        leases = [
            (ha.get("role", "active"), ha.get("epoch", 0), ha.get("holder"))
            for ha in (first, second)
        ]
        if leases[0] != leases[1]:
            return None, f"{name}'s lease changed between its two state reads"
        role, epoch, holder = leases[0]
        if role == "active":
            held.append({"router": name, "epoch": epoch, "holder": holder})
    if len(held) != 1:
        names = " and ".join(row["router"] for row in held) or "no router"
        return None, f"{names} reported control of the fleet"
    return held[0], ""


def control_problem(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """Why the fleet's control moved between two rounds, or an empty string."""
    if before["router"] != after["router"]:
        return (
            f"control moved from {before['router']} at epoch {before['epoch']} to "
            f"{after['router']} at epoch {after['epoch']}"
        )
    if before["epoch"] != after["epoch"] or before["holder"] != after["holder"]:
        return (
            f"{before['router']} took the lease again, from epoch {before['epoch']} to "
            f"{after['epoch']}"
        )
    return ""


# Prometheus series checks.

Query = Callable[[str], list[dict[str, Any]]]


def prometheus(origin: str, client: httpx.Client) -> Query:
    """A `Query` against the Prometheus HTTP API at `origin` through `client`, evaluated at the
    current time."""

    def query(expression: str) -> list[dict[str, Any]]:
        response = client.get(origin + "/api/v1/query", params={"query": expression})
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success" or body.get("data", {}).get("resultType") != "vector":
            raise ValueError(f"Prometheus answered {body.get('error') or body} for {expression}")
        return list(body["data"]["result"])

    return query


@contextmanager
def prometheus_at(socket: Path) -> Iterator[Query]:
    """A `Query` against the Prometheus forwarded to `socket`, whose client closes with the
    block."""
    with unix_client(socket, 30) as client:
        yield prometheus(LOCAL_ORIGIN, client)


def prometheus_started(socket: Path) -> float:
    """Seconds since the epoch at which the Prometheus forwarded to `socket` started, from its
    runtime information."""
    with unix_client(socket, 30) as client:
        response = client.get(LOCAL_ORIGIN + "/api/v1/status/runtimeinfo")
        response.raise_for_status()
        body = response.json()
    started = body.get("data", {}).get("startTime") if isinstance(body, dict) else None
    if body.get("status") != "success" or not isinstance(started, str):
        raise ValueError(f"Prometheus runtime information names no start time: {body}")
    return started_at_seconds(started)


@dataclass(frozen=True)
class Series:
    """One series check: a query whose every sample is 1 when the series holds.

    `expression` scopes its selectors with `@scope`, the fleet, and its ranges with `@window`,
    the trial's duration. `@scrapes` is the fewest samples a series scraped through the whole
    window holds, and `@controlling` matches the router that reported the active lease role at
    every scrape of the window. `<<name>>` takes each engine backend's dashboard query for
    `name`. `per` names the label whose values must each appear: `iid` for every engine, `role`
    for each pool. Without it the query returns one fleet-wide sample.
    """

    name: str
    meaning: str
    expression: str
    per: str | None = None


_ROUTER = 'job="narwhal-router",@scope'
# The router that held the active role at every scrape of the window. A standby's series and a
# router that took over partway through do not match it.
_ACTIVE = f'narwhal_router_role{{{_ROUTER},role="active"}}'
_CONTROLLING = (
    f"(min_over_time({_ACTIVE}[@window]) == 1 and count_over_time({_ACTIVE}[@window]) >= @scrapes)"
)
SERIES = (
    Series(
        "engine",
        "every engine's scrape target was up at every scrape of the trial",
        'min by (iid) (min_over_time(up{job="engines",@scope}[@window]) * '
        '(count_over_time(up{job="engines",@scope}[@window]) >= bool @scrapes))',
        "iid",
    ),
    Series(
        "request",
        "the routers served requests",
        f"sum(increase(narwhal_served_total{{{_ROUTER}}}[@window])) > bool 0",
    ),
    Series(
        "token",
        "the routers observed decode tokens",
        f"sum(increase(narwhal_decode_tokens_observed_total{{{_ROUTER}}}[@window])) > bool 0",
    ),
    Series("token", "the engines computed prompt tokens", "sum(<<prompt_tokens>>) > bool 0"),
    Series("token", "the engines generated output tokens", "sum(<<output_tokens>>) > bool 0"),
    # An engine may change pool during the trial, so its scrapes count across both roles.
    Series(
        "role",
        "the controlling router reported every engine in a pool at every scrape of the trial",
        "max by (iid) ((sum by (instance, iid) "
        f"(count_over_time(narwhal_instance_role{{{_ROUTER}}}[@window])) >= bool @scrapes) "
        "and on (instance) @controlling)",
        "iid",
    ),
    # A pool's load reads 0 while its engines run at their idle pace, as at a low rate, so the
    # check asks that the controlling router reported each pool's load throughout, not that the
    # load rose.
    Series(
        "pool-load",
        "the controlling router reported the prefill and decode pool loads at every scrape of "
        "the trial",
        "max by (role) ((count_over_time(narwhal_pool_load"
        f"{{{_ROUTER}}}[@window]) >= bool @scrapes) and on (instance) @controlling)",
        "role",
    ),
)
# The stack's Prometheus scrape interval, from its prometheus.yml.
SCRAPE_INTERVAL_S = 5
# Seconds after Prometheus starts by which it has scraped every target once: it opens its
# storage, then spreads the targets' first scrapes over one interval.
FIRST_SCRAPES_S = 2 * SCRAPE_INTERVAL_S
POOLS = ("prefill", "decode")


def _engine_series(name: str, selector: str, window: str, routers: str) -> str:
    """Every engine backend's dashboard query for `name`, joined with `or`, with the dashboard's
    variables replaced: the engine selector, the window, and the fleet's routers."""
    queries = []
    for backend in backend_names():
        metrics = load_backend(backend).metrics
        mapping = metrics.dashboard_series if metrics is not None else {}
        if name in mapping:
            query = (
                mapping[name]
                .replace("@sel", selector)
                .replace("$__rate_interval", window)
                .replace('instance=~"$router"', routers)
            )
            if "$" in query:
                raise ValueError(f"the {backend} {name} series keeps a dashboard variable")
            queries.append(f"({query})")
    if not queries:
        raise ValueError(f"no engine backend maps the {name} series")
    return "(" + " or ".join(queries) + ")"


def scrapes(window_s: int, scrape_s: int = SCRAPE_INTERVAL_S) -> int:
    """The fewest samples a series scraped every `scrape_s` seconds through the whole
    `window_s` holds: one less than the scrapes the window spans, for the alignment of the
    first scrape."""
    return max(1, window_s // scrape_s - 1)


def expression(series: Series, fleet: str, window_s: int, scrape_s: int = SCRAPE_INTERVAL_S) -> str:
    """The query of one check for `fleet` over the last `window_s` seconds, for series scraped
    every `scrape_s` seconds."""
    scope = f'fleet="{fleet}"'
    window = f"{window_s}s"
    text = series.expression.replace("@controlling", _CONTROLLING)
    while "<<" in text:
        start = text.index("<<")
        end = text.index(">>", start)
        engines = _engine_series(text[start + 2 : end], f'job="engines",{scope}', window, scope)
        text = text[:start] + engines + text[end + 2 :]
    return (
        text.replace("@scope", scope)
        .replace("@window", window)
        .replace("@scrapes", str(scrapes(window_s, scrape_s)))
    )


def check_series(
    query: Query,
    fleet: str,
    window_s: int,
    iids: Sequence[str],
    scrape_s: int = SCRAPE_INTERVAL_S,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run every series check; return each check's query, samples and verdict, and the
    problems of the checks that failed."""
    rows = []
    found = []
    for series in SERIES:
        text = expression(series, fleet, window_s, scrape_s)
        samples = query(text)
        values: dict[str, float] = {}
        for sample in samples:
            label = sample.get("metric", {}).get(series.per, "") if series.per else ""
            values[label] = float(sample["value"][1])
        expected = {"iid": tuple(iids), "role": POOLS, None: ("",)}[series.per]
        missing = [key for key in expected if values.get(key) != 1]
        problem = ""
        if missing:
            subject = ", ".join(missing) if series.per else "the fleet"
            problem = f"{series.name}: not shown for {subject}: {series.meaning}"
            found.append(problem)
        rows.append(
            {
                "series": series.name,
                "meaning": series.meaning,
                "query": text,
                "samples": samples,
                "passed": not missing,
                **({"problem": problem} if problem else {}),
            }
        )
    return rows, found
