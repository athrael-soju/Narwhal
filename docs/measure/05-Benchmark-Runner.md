# Ordered benchmark points

Start with a passing preflight for the current fleet, profiles, targets, and engine processes. Keep the router and engines running for inspection after the benchmark. Reserve the router for this run so its journal and counters can be reconciled with client outcomes.

Prepare the synthetic workload with [the load trial helper](03-Load-Trial.md#create-the-trial-directory-and-workload). Then write a private plan, for example `runs/benchmark-plan.json`:

```json
{
  "schema": 1,
  "points": [
    {
      "id": "rate-0_5",
      "workload": {"file": "runs/load-trial-EXAMPLE/workload/workload.json", "rate_rps": 0.5, "requests": 200},
      "client_argv": [
        ".venv/bin/python", "tools/measurement/load_trial.py", "run",
        "--base", "{base}", "--expected-model", "{model}",
        "--workload", "runs/load-trial-EXAMPLE/workload/workload.json",
        "--rate", "0.5", "--requests", "200",
        "--ttft", "2", "--tpot", "0.0333", "--attainment", "0.95",
        "--out", "{point_dir}/client"
      ],
      "client_timeout_s": 900,
      "drain_timeout_s": 180
    },
    {
      "id": "rate-1",
      "workload": {"file": "runs/load-trial-EXAMPLE/workload/workload.json", "rate_rps": 1, "requests": 200},
      "client_argv": [
        ".venv/bin/python", "tools/measurement/load_trial.py", "run",
        "--base", "{base}", "--expected-model", "{model}",
        "--workload", "runs/load-trial-EXAMPLE/workload/workload.json",
        "--rate", "1", "--requests", "200",
        "--ttft", "2", "--tpot", "0.0333", "--attainment", "0.95",
        "--out", "{point_dir}/client"
      ],
      "client_timeout_s": 900,
      "drain_timeout_s": 180
    }
  ]
}
```

Replace the example workload path with the prepared file. Both point definitions declare the workload and the exact external client invocation. Arguments are passed directly to a process, without a shell. The runner substitutes `{base}`, `{model}`, `{point_id}`, and `{point_dir}`. Keep credentials in an environment variable, never in `client_argv` or the router URL.

Run from the repository root through the private router tunnel:

```bash
umask 077
.venv/bin/python tools/measurement/benchmark_runner.py \
  --base http://127.0.0.1:18000 \
  --model '<served-model>' \
  --plan runs/benchmark-plan.json \
  --out runs/benchmark-001
```

If ingress requires a bearer token, add `--api-key-env NAME` after exporting `NAME`. The runner uses that token for its own probes; configure the external client separately to use the same private ingress. Do not place a token in the plan.

The runner creates a fresh private output directory and retains `manifest.json` with the plan, its SHA-256 digest, the runner digest, model, URL, and invocation. For each point it checks `/ready` and `/v1/models`, waits for zero admission and resident work, starts the client, then waits for the fleet to drain. It retains `result.json`, `client.stdout`, and `client.stderr` under the point directory. The result records readiness, client exit status, initial and final drain condition, timestamps, and the last state snapshot. A refused readiness check, model mismatch, nonzero client exit, or drain timeout stops the sequence. A nonzero client exit still gets a drain attempt. The helper never stops the router or engines.

The existing load trial helper exits `2` when candidate attainment or scheduling validity fails. The runner records that status and stops. Inspect its `summary.json` and `requests.jsonl` before deciding whether the rate is a valid measured miss or the client needs repair.
