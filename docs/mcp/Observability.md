# Inspect monitoring through MCP

The unreleased MCP server provides `monitoring_status`, `metrics_query`,
`host_inventory` and `host_logs` for registered targets. Each call requires the
`inspect` capability. These tools read evidence and retain redacted artifacts;
`monitoring_start` is a separate planned fleet action.

The installed provider reads local dev inputs and the `local` host. Fleet host
access requires the SSH site provider. Registered Prometheus queries use the
configured HTTP endpoint for either target kind.

## Register monitoring inputs

Start with an existing [target registration](Registration.md). Set
`endpoints.router_env`, `endpoints.prometheus_env` and `endpoints.grafana_env` to
names of environment variables supplied to the MCP server. Each value must be
an HTTP or HTTPS origin, without a username, password, path, query or fragment.
The local monitoring target builder requires HTTP router and engine origins
with explicit ports, matching the shipped scrape configuration.

The following fields illustrate an addition to an existing dev target. Replace
the environment names and log path with values for that target:

```json
{
  "endpoints": {
    "router_env": "DEV_ROUTER_URL",
    "prometheus_env": "DEV_PROMETHEUS_URL",
    "grafana_env": "DEV_GRAFANA_URL"
  },
  "queries": [
    {
      "id": "router-ready",
      "kind": "instant",
      "expression": "narwhal_router_ready{job=\"narwhal-router\"}"
    },
    {
      "id": "router-ready-history",
      "kind": "range",
      "expression": "narwhal_router_ready{job=\"narwhal-router\"}"
    }
  ],
  "logs": [
    {"id": "router", "host_id": "local", "source": "/srv/narwhal/runs/dev/router.log"}
  ]
}
```

Choose PromQL expressions that select only the target's fleet. Clients select
an existing query ID; they cannot supply PromQL, label substitutions, URLs,
paths or host destinations. Restart the server after editing its registry.

## Check monitoring and serving readiness

Call `monitoring_status` with the registered `target_id`. For a dev target, the
provider reads the instance's saved `fleet.json` to obtain engine IDs and scrape
addresses. It compares those addresses with Prometheus's active targets.

The tool checks:

- Prometheus readiness and version `3.14.0`.
- Exactly one healthy configured router scrape and every configured engine
  scrape, with no missing, duplicate or unexpected engine identity.
- Fresh scrape timestamps and a fresh `narwhal_router_ready` sample equal to `1`.
- Grafana health and version `13.2.1`, its Prometheus datasource URL and the
  shipped `narwhal-router` dashboard's router selection and query expressions.
- The router's `/ready` response, independently of scrape health.

The result contains each source observation and `readiness: pass|fail|unknown`.
Read the `monitoring_readiness` source for individual checks. HTTP 503 from
`/ready` records a failed serving check. When all evidence was collected, the
tool can return `outcome: success` with `readiness: fail`. An unavailable source
makes its dependent checks `unknown`; another completed failure still makes
the overall readiness `fail`.

Every status source has five seconds within the call's `timeout_s` deadline
and a 262,144-byte body limit. The server compares observation and scrape times
with `freshness_s`. Failed or stale observations remain in the returned evidence.
The checks establish monitoring configuration and observed readiness; deployment
qualification still requires the measured gates in [Deploy](../Deploy.md).

## Query registered metrics

Call `metrics_query` with `target_id` and `query_id`. An instant query accepts no
range fields. A range query requires UTC `start` and `end` timestamps ending in
`Z`, with `start < end`, a window of at most 3600 seconds and no future times.
Its `step_s` defaults to `15` and accepts `1..300`.

The tool returns at most 100 series and 10,000 samples. Set `limit_series` to
`1..100` to lower the series cap. It preserves Prometheus sample values as
strings, including `NaN` and infinities. Such values cannot qualify a measurement.
Historical range samples are evaluated within the requested window; their age
relative to the present does not make a historical query stale.

Warnings, missing samples, stale instant samples and truncated responses produce
`outcome: degraded` and `complete: false`. The HTTP body limit is 8,388,608 bytes.
The server disables redirects and environment proxies, requests uncompressed
responses and rejects compressed bodies. It exports retained redacted response
prefixes when a source exceeds its limit. Large normalized results are also
exported under the [common response bound](Tools.md#common-argument-rules).

## Inspect host evidence

For a dev target, call `host_inventory` with `host_id: local`. The provider
collects operating-system and CPU identity, `ip -json address`, and GPU inventory
from `rocminfo` when `/dev/kfd` exists or `nvidia-smi` otherwise. A missing or
failed utility produces an incomplete source record; the provider does not
infer the absent hardware information. Inventory source content has an aggregate
1,048,576-byte limit. The returned `snapshot_artifact_id` identifies a manifest
that links each source's artifact and records its collection status.

Call `host_logs` with a registered `log_id` and optional `max_bytes` in
`1..65536`, default `65536`. The provider captures that many trailing bytes from
a regular UTF-8 file, adjusting the start to a character boundary. It rejects
symlinks, unsafe ownership or writable permissions, credential files and binary
content. If the source shortens during capture, the result is incomplete.

Both tools redact credentials and request content before exporting files.
`host_logs` has no request-content opt-in. Read an export through `artifact_read`
using the same target ID, and follow `next_offset` until it is `null`. Later
changes to the original source do not change the retained artifact.
