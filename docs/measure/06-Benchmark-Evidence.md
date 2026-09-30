# Benchmark evidence bundle

The `evidence` object in the plan starts one evidence collector for each point.

Run the [ordered benchmark runner](05-Benchmark-Runner.md) on a host that:

- reads the router's append-only JSONL request journal as a local file
- reaches the router and every engine metrics endpoint

Point `journal_path` at the file `narwhal-serve --journal` writes. The point's `journal-rows.json` holds the rows between two byte offsets, recorded before the client starts and after the final drain.

Put `evidence` at the top level of the plan, next to `schema` and `points`:

```json
{
  "journal_path": "runs/deployment/journal.jsonl",
  "fleet_path": "runs/deployment/fleet.json",
  "profiles_path": "profiles/accepted.json",
  "sample_interval_s": 1,
  "engine_metrics_urls": {
    "engine-0": "http://127.0.0.1:8010/metrics",
    "engine-1": "http://127.0.0.1:8011/metrics"
  },
  "identity": {
    "narwhal_revision": "<40-character-commit>",
    "model_id": "<served-model>",
    "benchmark_client_version": "<client-version-or-commit>",
    "engine_image": "<pinned-image-with-digest>",
    "engine_version": "<engine-version>",
    "checkpoint_revision": "<checkpoint-commit>",
    "gpu_shape": "<GPU-model-and-count>",
    "gpu_allocation": "<private-host-and-GPU-map>",
    "initial_role_split": "<prefill/decode assignment>"
  }
}
```

- Replace the example metrics URLs with addresses the runner host can reach.
- Check the self-declared `identity` fields against the deployment and preflight records before you accept a GPU result.
- Keep the plan and everything under `runs/` private.

The collector records:

| Input | Collector record |
| --- | --- |
| Fleet file and profiles | SHA-256 digests before each point, and a report of any change during the point |
| Retained client records, warmup, and summary files | SHA-256 digests |
| `journal_run` in the router state | Metrics samples grouped by process, across restarts and standby takeovers |

For each point, the runner writes these files under `runs/<run>/<point>/`:

| File | Contents |
| --- | --- |
| `result.json` | Readiness, client exit status, drain result, timestamps, and evidence diagnostic count |
| `evidence.json` | Journal cursors, identity, file digests, counts by terminal class and process run, counter deltas, role timeline, and diagnostics |
| `samples.json` | Timestamped router state, router `/metrics`, engine metrics, and scrape errors from the load and drain interval |
| `journal-rows.json` | All JSONL rows between the two byte offsets, including terminal rows, process metadata, and events |
| `client/requests.jsonl`, `client/warmup.json`, `client/summary.json` | Raw client files when the client writes inside the point directory |
| `client-*.snapshot.*` | Exact private copies when the configured client files live outside the point directory |
| `client.stdout`, `client.stderr` | Exact external client output |
| `summary.shareable.json` | Selected counts, latency and throughput when the client wrote `summary.json`, anonymized role history, configuration digests, and evidence gaps |

`summary.shareable.json` is the redacted copy for publication. Every other file stays private. Redaction removes URLs, credentials, private file paths, engine IDs, request failure text, and the raw engine image reference. Before you publish, check the model and revision labels you supplied for sensitive data.

The collector runs these comparisons:

| Comparison | Scope |
| --- | --- |
| Sent-client totals against terminal-journal totals | Every point |
| Completed totals, client against journal | Every point |
| Client offers against journal terminals, joined on `client_rid` | Points where both sides supply IDs |
| Terminal classes against router counter deltas | Each `journal_run`, from that process's baseline and final sample |

Router counters at process start:

| Router counters | At process start |
| --- | --- |
| `narwhal_offered_total`, `narwhal_expired_total`, `narwhal_invalid_requests_total` | Reset |
| Served, failed, refused, rejected, and cancelled | Can be restored |

A process change with an incomplete sample pair raises a counter diagnostic.

The role timeline holds the observed role pools and each newly observed flip record.

| Diagnostic | Raised when | Reports |
| --- | --- | --- |
| `role_history_gap` | Bounded state history lost a flip that `narwhal_flips_total` counts | Point |
| `scrape_error` | A scrape fails | Point |
| `scrape_interval_gap` | An interval between samples exceeds 2.5 times the declared sample cadence | Point and time window |

For a mismatch:

1. Inspect `evidence.json`.
2. Inspect the bounded `journal-rows.json`, the client files named by `retained_client_files`, and the raw `samples.json`.
3. Check for unrelated clients on the router, a process restart, a failed scrape, or omitted client outcomes.
4. Keep the failed bundle.
5. Fix the cause.
6. Run the point again.
