# Ordered benchmark points

`benchmark_runner.py` runs the plan's points in order. Before and after each point's client it checks `/ready` and `/v1/models` and waits for the router to go idle and drain.

Prerequisites:

- A passing preflight against the current fleet.
- A router reserved for this run.
- Leave the router and engines running after the benchmark so you can inspect them.

| Plan                    | Runner host                                                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| With an `evidence` object | A host that reads the router's journal file and reaches every engine metrics endpoint; see [Benchmark evidence bundle](06-Benchmark-Evidence.md) |
| Other plans             | The workstation repository root, through the private router tunnel                                                                          |

1. Prepare the workload with the [load trial helper](03-Load-Trial.md#create-the-trial-directory-and-workload).
2. Write a private plan that points `workload.file` and `--workload` at the prepared file, for example `runs/benchmark-plan.json`:

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

3. Run the plan through the private router tunnel:

    ```bash
    umask 077
    .venv/bin/python tools/measurement/benchmark_runner.py \
      --base http://127.0.0.1:18000 \
      --model '<served-model>' \
      --plan runs/benchmark-plan.json \
      --out runs/benchmark-001
    ```

The runner executes `client_argv` directly as an argument vector. It substitutes `{base}`, `{model}`, `{point_id}`, and `{point_dir}`.

Put credentials in an environment variable.

For bearer-token ingress, export the token and pass its variable name with `--api-key-env NAME`. The token authenticates the runner's probes. Give the external client its own credentials.

The runner writes a fresh private output directory:

| File                             | Location        | Contents                                                                                        |
| -------------------------------- | --------------- | ----------------------------------------------------------------------------------------------- |
| `manifest.json`                  | Output root     | Plan, its SHA-256 digest, runner digest, model, URL, and invocation                            |
| `result.json`                    | Point directory | Readiness, client exit status, initial and final drain condition, timestamps, last state snapshot |
| `client.stdout`, `client.stderr` | Point directory | External client output                                                                          |

The runner stops on a refused readiness check, a model mismatch, a nonzero client exit, or a drain timeout. After a nonzero client exit it tries to drain first.

Load trial helper exit `2` (see [Measure 0.5 request/s](03-Load-Trial.md#8-measure-05-requests) for the exit codes) means candidate attainment or scheduling validity failed. The runner records it and stops. Read `summary.json` and `requests.jsonl` to tell a valid measured miss from a client that needs repair.
