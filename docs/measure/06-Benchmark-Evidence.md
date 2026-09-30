# Benchmark evidence bundle

The `evidence` object in the plan starts one evidence collector for each point.

Run the [ordered benchmark runner](05-Benchmark-Runner.md) on a host that:

- reads the router's append-only JSONL request journal as a local file
- reaches the router and every engine metrics endpoint

Set `journal_path` to the `narwhal-serve --journal` file.

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

1. Replace the example metrics URLs with addresses the runner host can reach.
2. Check the self-declared `identity` fields against the deployment and preflight records before accepting a GPU result.
3. Keep the plan and everything under `runs/` private.

| Input | Collector record |
| --- | --- |
| Fleet file and profiles | SHA-256 digests before each point and a report of changes during the point |
| Retained client records, warmup, and summary files | SHA-256 digests |
| `journal_run` in the router state | Metrics samples grouped by process, across restarts and standby takeovers |

Files under `runs/<run>/<point>/`:

| File | Contents |
| --- | --- |
| `result.json` | Readiness, client exit status, drain result, timestamps, evidence diagnostic count |
| `evidence.json` | Journal cursors, identity, file digests, counts by terminal class and process run, counter deltas, role timeline (observed role pools and flip records), diagnostics |
| `samples.json` | Timestamped router state, router `/metrics`, engine metrics, scrape errors from the load and drain interval |
| `journal-rows.json` | JSONL rows between the byte offset before client start and the byte offset after final drain: terminal rows, process metadata, events |
| `client/requests.jsonl`, `client/warmup.json`, `client/summary.json` | Client files when the client writes inside the point directory |
| `client-*.snapshot.*` | Private copies when the configured client files live outside the point directory |
| `client.stdout`, `client.stderr` | External client output |
| `summary.shareable.json` | Redacted publication copy: selected counts, anonymized role history, configuration digests, evidence gaps, and latency and throughput when the client wrote `summary.json` |

1. Publish `summary.shareable.json`, which redacts URLs, credentials, private file paths, engine IDs, request failure text and the raw engine image reference.
2. Check the model and revision labels you supplied for sensitive data before publication.

| Comparison | Scope |
| --- | --- |
| Sent-client totals against terminal-journal totals | Every point |
| Completed totals, client against journal | Every point |
| Client offers against journal terminals, joined on `client_rid` | Points where both sides supply IDs |
| Terminal classes against router counter deltas | Each `journal_run`, from that process's baseline and final sample |

| Router counters | At process start |
| --- | --- |
| `narwhal_offered_total`, `narwhal_expired_total`, `narwhal_invalid_requests_total` | Reset |
| Served, failed, refused, rejected, and cancelled | May be restored |

| Diagnostic | Raised when | Reports |
| --- | --- | --- |
| `role_history_gap` | Bounded state history lost a flip that `narwhal_flips_total` counts | Point |
| `scrape_error` | A scrape fails | Point |
| `scrape_interval_gap` | An interval between samples exceeds 2.5 times `sample_interval_s` | Point and time window |
| `counter_missing` | A counter is absent from the first or last sample of a `journal_run` | Run and metric |
| `counter_reset` | A counter delta is negative | Run and metric |

For a mismatch:

1. Inspect `evidence.json`.
2. Inspect `journal-rows.json`.
3. Inspect the client files named by `retained_client_files`.
4. Inspect `samples.json`.
5. Check for unrelated clients on the router, a process restart, a failed scrape, or omitted client outcomes.
6. Keep the failed bundle.
7. Fix the cause.
8. Run the point again.
