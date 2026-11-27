"""The capacity trial of `narwhal deploy run --trial`: its definition, its prerequisites and the
Prometheus series it checks.

The trial definition is a `narwhal.capacity-trial` document: the synthetic workload, the
offered rates and the thresholds each rate must meet. The series checks are data, so
`docs/deploy/07-Serve-and-Measure.md` lists them and promtool tests evaluate them. Every query
goes through a `Query`, a callable that returns Prometheus's instant vector for an expression,
so tests replace Prometheus with fixtures.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from narwhal.backends import load as load_backend
from narwhal.backends import names as backend_names

from ..contracts import TRIAL, ContractVersionError, validate_document

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


def load(path: Path) -> Definition:
    """Read and check the trial definition at `path`; a ValueError names every problem."""
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"--trial {path}: {error}") from None
    if found := problems(document):
        raise ValueError(f"--trial {path}: " + "; ".join(found))
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


# Router samples, as the benchmark evidence collector takes them, for the reconciliation.


def router_sample(client: httpx.Client, origin: str) -> dict[str, Any]:
    """One router's state, metrics and state again; the second state read vouches that the
    metrics belong to the same journal run."""
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
            response = client.get(origin + path)
            response.raise_for_status()
            item[name] = response.json() if decoded else response.text
        except (httpx.HTTPError, ValueError) as error:
            item["errors"][name] = str(error)
    if "state_after" in item:
        item["journal_run_after"] = item.pop("state_after").get("journal_run")
    elif "state_after" in item["errors"]:
        item["journal_run_after"] = None
    return item


# Prometheus series checks.

Query = Callable[[str], list[dict[str, Any]]]


def prometheus(origin: str, client: httpx.Client | None = None) -> Query:
    """A `Query` against the Prometheus HTTP API at `origin`, evaluated at the current time."""
    session = client or httpx.Client(timeout=30, trust_env=False)

    def query(expression: str) -> list[dict[str, Any]]:
        response = session.get(origin + "/api/v1/query", params={"query": expression})
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success" or body.get("data", {}).get("resultType") != "vector":
            raise ValueError(f"Prometheus answered {body.get('error') or body} for {expression}")
        return list(body["data"]["result"])

    return query


@dataclass(frozen=True)
class Series:
    """One series check: a query whose every sample is 1 when the series holds.

    `expression` scopes its selectors with `@scope`, the fleet, and its ranges with `@window`,
    the trial's duration. `<<name>>` takes each engine backend's dashboard query for `name`.
    `per` names the label whose values must each appear: `iid` for every engine, `role` for
    each pool. Without it the query returns one fleet-wide sample.
    """

    name: str
    meaning: str
    expression: str
    per: str | None = None


_ROUTER = 'job="narwhal-router",@scope'
SERIES = (
    Series(
        "engine",
        "every engine's scrape target was up at every scrape of the trial",
        'min by (iid) (min_over_time(up{job="engines",@scope}[@window]))',
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
    Series(
        "role",
        "every engine held a role in a pool",
        f"max by (iid) (last_over_time(narwhal_instance_role{{{_ROUTER}}}[@window]))",
        "iid",
    ),
    Series(
        "pool-load",
        "the prefill and decode pools each carried load",
        f"max by (role) (max_over_time(narwhal_pool_load{{{_ROUTER}}}[@window])) > bool 0",
        "role",
    ),
)
POOLS = ("prefill", "decode")


def _engine_series(name: str, selector: str, window: str) -> str:
    """Every engine backend's dashboard query for `name`, joined with `or`."""
    queries = []
    for backend in backend_names():
        metrics = load_backend(backend).metrics
        mapping = metrics.dashboard_series if metrics is not None else {}
        if name in mapping:
            query = mapping[name].replace("@sel", selector).replace("$__rate_interval", window)
            queries.append(f"({query})")
    if not queries:
        raise ValueError(f"no engine backend maps the {name} series")
    return "(" + " or ".join(queries) + ")"


def expression(series: Series, fleet: str, window_s: int) -> str:
    """The query of one check for `fleet` over the last `window_s` seconds."""
    scope = f'fleet="{fleet}"'
    window = f"{window_s}s"
    text = series.expression
    while "<<" in text:
        start = text.index("<<")
        end = text.index(">>", start)
        engines = _engine_series(text[start + 2 : end], f'job="engines",{scope}', window)
        text = text[:start] + engines + text[end + 2 :]
    return text.replace("@scope", scope).replace("@window", window)


def check_series(
    query: Query, fleet: str, window_s: int, iids: Sequence[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run every series check; return each check's query, samples and verdict, and the
    problems of the checks that failed."""
    rows = []
    found = []
    for series in SERIES:
        text = expression(series, fleet, window_s)
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
