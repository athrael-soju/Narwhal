---
description: Evidence collection for each point of a Narwhal benchmark plan.
---

# Benchmark evidence bundle

The `evidence` object in the plan starts one evidence collector for each point of the [ordered benchmark runner](05-Benchmark-Runner.md).

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

| Field | Value |
| --- | --- |
| `journal_path` | The `narwhal-serve --journal` file |
| `fleet_path` | Fleet file under test |
| `profiles_path` | Profile store under test |
| `sample_interval_s` | Positive sampling interval in seconds |
| `engine_metrics_urls` | Engine name mapped to its HTTP metrics URL |
| `identity` | All nine labels shown in the example |
| `client_records` | Optional client records path, default `{point_dir}/client/requests.jsonl` |

The collector reads `warmup.json` and `summary.json` from the directory that holds the client records.

1. Replace the example metrics URLs with addresses the runner host can reach.
2. Check the self-declared `identity` fields against the deployment and preflight records before accepting a GPU result.
3. Keep the plan and everything under `runs/` private.

| Input | Collector record |
| --- | --- |
| Fleet file and profiles | SHA-256 digests before each point and a report of changes during the point |
| Retained client records, warmup, and summary files | SHA-256 digests |
| `journal_run` in the router state | Metrics samples grouped by process, across restarts and standby takeovers |

## Point files

The collector adds these files under `runs/<run>/<point>/`:

| File | Contents |
| --- | --- |
| `evidence.json` | Journal cursors, identity, file digests, counts by terminal class and process run, counter deltas, role timeline (observed role pools and flip records), diagnostics |
| `samples.json` | Timestamped router state, router `/metrics`, engine metrics, scrape errors from the load and drain interval |
| `journal-rows.json` | JSONL rows between the byte offset before client start and the byte offset after final drain: terminal rows, process metadata, events |
| `client/requests.jsonl`, `client/warmup.json`, `client/summary.json` | Client files when the client writes inside the point directory |
| `client-*.snapshot.*` | Private copies when the configured client files live outside the point directory |
| `summary.shareable.json` | Redacted publication copy: selected counts, anonymized role history, configuration digests, evidence gaps, and latency and throughput when the client wrote `summary.json` |

| Private value | In `summary.shareable.json` |
| --- | --- |
| URLs, credentials, private file paths, request failure text | Removed |
| Engine IDs | Replaced by `engine-1`, `engine-2`, and onward |
| Engine image reference, GPU allocation | Replaced by their SHA-256 digests |

Before publishing `summary.shareable.json`, check the model and revision labels you supplied for sensitive data.

## Reconciliation

| Comparison | Scope | Diagnostic on mismatch |
| --- | --- | --- |
| Sent-client totals against terminal-journal totals | Every point | `client_journal_count` |
| Completed totals, client against journal | Every point | `completed_count` |
| Client offers against journal terminals, joined on `client_rid` | Points where both sides supply IDs | `client_journal_ids` |
| Terminal classes against router counter deltas | Each `journal_run`, from that process's baseline and final sample | `counter_mismatch` |

| Router counters | At process start |
| --- | --- |
| `narwhal_offered_total`, `narwhal_expired_total`, `narwhal_invalid_requests_total` | Reset |
| Served, failed, refused, rejected, and cancelled | Restored from the saved handoff state |

| Diagnostic | Raised when | Reports |
| --- | --- | --- |
| `journal_client_ids_missing` | Client rows carry IDs and some journal terminal rows have an empty or absent `client_rid` | Point |
| `missing_run_metrics` | A journal run has zero router metrics samples | Point and run |
| `role_history_gap` | Bounded state history lost a flip that `narwhal_flips_total` counts | Point |
| `scrape_error` | A scrape fails | Point, time, and targets |
| `scrape_interval_gap` | An interval between samples exceeds 2.5 times `sample_interval_s` | Point and time window |
| `counter_missing` | A counter is absent from the first or last sample of a `journal_run` | Run and metric |
| `counter_reset` | A counter delta is negative | Run and metric |
| `input_changed` | The fleet or profile file digest changed during the point | Point and input |
| `journal_replaced` | The journal file's device or inode changed during the point | Start and end cursors |
| `journal_truncated` | The journal file shrank during the point | Start and end cursors |
| `journal_parse_error` | A journal line fails JSON parsing | Line number |
| `client_records_missing` | The client records file is absent | Point |
| `client_parse_error` | A client record line fails JSON parsing | Point and line number |
| `warmup_parse_error` | `warmup.json` fails JSON parsing | Point |

For a mismatch:

1. Inspect `evidence.json`.
2. Inspect `journal-rows.json`.
3. Inspect the client files named by `retained_client_files`.
4. Inspect `samples.json`.
5. Check for unrelated clients on the router, a process restart, a failed scrape, or omitted client outcomes.
6. Keep the failed bundle.
7. Fix the cause.
8. Run the point again.
