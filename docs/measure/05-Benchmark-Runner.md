# Ordered benchmark points

`benchmark_runner.py` runs the plan's points in order, with `/ready`, `/v1/models`, and idle-router drain checks before and after each point's client.

Prerequisites:

- A passing preflight against the current fleet.
- A router reserved for this run.
- The router and engines stay running after the benchmark.

| Plan                    | Runner host                                                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| With an `evidence` object | A host that reads the router's [journal file](06-Benchmark-Evidence.md) and reaches every engine metrics endpoint |
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

`client_argv` is an argument vector that takes the `{base}`, `{model}`, `{point_id}`, and `{point_dir}` placeholders.

For bearer-token ingress, the token authenticates the runner's probes:

1. Export the token in an environment variable.
2. Pass the variable name with `--api-key-env NAME`.
3. Give the external client separate credentials.

The runner writes a fresh private output directory:

| File                             | Location        | Contents                                                                                        |
| -------------------------------- | --------------- | ----------------------------------------------------------------------------------------------- |
| `manifest.json`                  | Output root     | Plan, its SHA-256 digest, runner digest, model, URL, and invocation                            |
| `result.json`                    | Point directory | Readiness, client exit status, initial and final drain condition, timestamps, last state snapshot |
| `client.stdout`, `client.stderr` | Point directory | External client output                                                                          |

The runner stops on:

- a refused readiness check
- a model mismatch
- a nonzero client exit, after a drain attempt
- a drain timeout

For client exit `2` in the load trial helper's [exit codes](03-Load-Trial.md#8-measure-05-requests), read `summary.json` and `requests.jsonl`:

| `client_schedule_valid` in `summary.json` | Exit `2` means |
| --- | --- |
| `true` | A valid measured miss |
| `false` | A client that needs repair |
