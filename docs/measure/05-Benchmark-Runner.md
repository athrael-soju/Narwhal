# Benchmark runner

The benchmark runner automates the rate sweep from the [synthetic load trial](03-Load-Trial.md). A plan lists each rate, and the runner executes the points in order. It stops at the first failure and leaves the router and engines running.

## Prerequisites

- A passing preflight for the current fleet, profiles, targets, and engine processes.
- The router reserved for this run, so its journal and counters line up with the client's records.
- A workload prepared with [the load trial helper](03-Load-Trial.md#create-the-trial-directory-and-workload).

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

Replace the example workload path with your own. Each point names its workload and the client command to run. The runner fills in `{base}`, `{model}`, `{point_id}`, and `{point_dir}`, then starts the command directly with no shell. `client_argv` must include both `{base}` and `{model}`. Point IDs contain only letters, digits, hyphens, and underscores. Every point needs a positive `client_timeout_s` and `drain_timeout_s`.

## Run it

Run this from the repository root, connecting through the private router tunnel:

```bash
umask 077
.venv/bin/python tools/measurement/benchmark_runner.py \
  --base http://127.0.0.1:18000 \
  --model '<served-model>' \
  --plan runs/benchmark-plan.json \
  --out runs/benchmark-001
```

Do not put credentials in the plan, in `client_argv`, or in the router URL. If ingress requires a bearer token, export it in an environment variable and add `--api-key-env NAME`. The runner uses the token only for its own `/ready` and `/v1/models` probes. Configure the external client separately to use the same private ingress.

## What the runner does

For each point, the runner checks `/ready` and `/v1/models`, waits until the router has no admission or resident work, starts the client, and waits for the fleet to drain.

The runner stops if the readiness check fails, the model does not match, the client exits nonzero, or a drain times out. A failed client still gets a drain attempt.

The load trial helper exits `2` when it misses the attainment target or loses its schedule, and the runner stops. `summary.json` and `requests.jsonl` show which happened, as described in [the load trial](03-Load-Trial.md).

## Output

The runner creates the `--out` directory with private permissions. It fails if the directory already exists.

- `manifest.json`: the plan, the SHA-256 digest of the plan file, the SHA-256 digest of the runner script, the model, the URL, and the invocation.
- One directory per point, containing `result.json`, `client.stdout`, and `client.stderr`.

`result.json` records readiness, client exit status, drain state before and after the point, timestamps, and the last state snapshot.
