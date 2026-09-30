# Ordered benchmark points

The benchmark runner automates the rate sweep from the [synthetic load trial](03-Load-Trial.md). You write a plan listing each rate. For each one, the runner checks that the router is ready, runs the client, and waits for the fleet to drain. It stops at the first problem and never stops the router or engines, so you can inspect them afterward.

Before you start:

- have a passing preflight for the current fleet, profiles, targets, and engine processes
- reserve the router for this run, so its journal and counters can be matched to the client's records
- prepare the workload with [the load trial helper](03-Load-Trial.md#create-the-trial-directory-and-workload)

## Write the plan

Save a private plan, for example `runs/benchmark-plan.json`:

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

Replace the example workload path with your own. Each point names its workload and the exact client command to run. The runner fills in `{base}`, `{model}`, `{point_id}`, and `{point_dir}`, then starts the command directly, without a shell. `client_argv` must include both `{base}` and `{model}`. Point IDs can contain only letters, digits, hyphens, and underscores, and every point needs a positive `client_timeout_s` and `drain_timeout_s`.

Keep credentials in an environment variable. They don't belong in the plan, in `client_argv`, or in the router URL.

## Run it

From the repository root, through the private router tunnel:

```bash
umask 077
.venv/bin/python tools/measurement/benchmark_runner.py \
  --base http://127.0.0.1:18000 \
  --model '<served-model>' \
  --plan runs/benchmark-plan.json \
  --out runs/benchmark-001
```

If ingress needs a bearer token, export it and add `--api-key-env NAME`. The runner only uses the token for its own probes. Configure the external client separately to use the same private ingress.

## What the runner does

For each point, the runner checks `/ready` and `/v1/models`, waits until there's no admission or resident work, starts the client, and then waits for the fleet to drain.

The sequence stops if the readiness check fails, the model doesn't match, the client exits nonzero, or a drain times out. A client that fails still gets a drain attempt.

The load trial helper exits `2` when it misses the attainment target or loses its schedule, so the runner stops there too. Use `summary.json` and `requests.jsonl` to work out which one it was, as described in the [load trial's exit codes](03-Load-Trial.md).

## Output

The runner creates a fresh private output directory. `manifest.json` holds the plan and its SHA-256 digest, the runner's digest, the model, the URL, and the invocation. Each point gets its own directory with `result.json`, `client.stdout`, and `client.stderr`. The result records readiness, client exit status, drain state before and after the point, timestamps, and the last state snapshot.
