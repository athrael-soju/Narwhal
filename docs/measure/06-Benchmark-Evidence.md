# Benchmark evidence bundle

Each point gets its own evidence collector, started from the `evidence` object in the plan. Run the [ordered benchmark runner](05-Benchmark-Runner.md) on a host that can read the router's append-only JSONL request journal as a local file. The host also needs to reach the router and every engine metrics endpoint. Point `journal_path` at the file `narwhal-serve --journal` writes. Older rows in that file are fine. The collector records byte offsets before the client starts and after the final drain. Only the rows between them go to the point's `journal-rows.json`.

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

Replace the example metrics URLs with addresses the runner host can reach. You declare the identity fields yourself, so check them against the deployment and preflight records before you accept a GPU result. The plan and everything under `runs/` stay private.

Before each point the collector takes SHA-256 digests of the fleet file and profiles, and reports a change during the point. It hashes the retained client records, warmup and summary files too. `journal_run` in the router state lets it group metrics samples by process, including across a restart or standby takeover.

For each point, the runner writes these files under `runs/<run>/<point>/`:

| File | Contents |
| --- | --- |
| `result.json` | Readiness, client exit status, drain result, timestamps, and evidence diagnostic count |
| `evidence.json` | Journal cursors, identity, file digests, counts by terminal class and process run, counter deltas, role timeline, and diagnostics |
| `samples.json` | Timestamped router state, router `/metrics`, engine metrics, and scrape errors from the load and drain interval |
| `journal-rows.json` | All JSONL rows between the two byte offsets, including terminal rows, process metadata, and events |
| `client/requests.jsonl`, `client/warmup.json`, `client/summary.json` | Raw client files when the client writes inside the point directory; `evidence.json` names the retained files and records their hashes |
| `client-*.snapshot.*` | Exact private copies when the configured client files live outside the point directory |
| `client.stdout`, `client.stderr` | Exact external client output |
| `summary.shareable.json` | Selected counts, latency and throughput when the client wrote `summary.json`, anonymized role history, configuration digests, and evidence gaps |

The shareable summary is a redacted copy meant for publication. It leaves out URLs, credentials, private file paths, engine IDs, request failure text and the raw engine image reference. Read it before you publish, because a model or revision label you supplied could carry sensitive data. Every other file stays private.

The collector joins client offers to journal terminals on `client_rid` when both sides supply IDs. Without IDs it compares sent-client and terminal-journal totals. Completed totals get their own comparison. For each `journal_run`, it compares terminal classes with router counter deltas calculated from that process's baseline and final sample. The counters `narwhal_offered_total`, `narwhal_expired_total`, and `narwhal_invalid_requests_total` reset at process start. The served, failed, refused, rejected, and cancelled counters may be restored. A process change without a full sample pair raises a counter diagnostic.

The role timeline begins with the observed role pools and adds the newly observed flip records. The collector compares those flips with `narwhal_flips_total` and flags a `role_history_gap` when bounded state history has lost a change. It also flags scrape errors and any interval longer than 2.5 times the declared sample cadence. Each diagnostic names its point. Scrape-interval diagnostics also give the time window.

For a mismatch, inspect `evidence.json`, then the bounded `journal-rows.json`, the client files named by `retained_client_files`, and the raw `samples.json`. The unscored warmup remains in its own client file. Check whether unrelated clients used the router, a process restarted, a scrape failed, or the client omitted outcomes. Keep the failed bundle, fix the cause, and run the point again. Editing the summary does not close a gap.
