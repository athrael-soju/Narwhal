# Ordered benchmark points

`benchmark_runner.py` runs the plan's points in order, with `/ready`, `/v1/models`, and idle-router drain checks before and after each point's client.

Prerequisites:

- A passing preflight against the current fleet.
- A router reserved for this run.
- A router and engines that stay running through the benchmark.

A plan with an `evidence` object runs on a host that:

- Reads the router's [journal file](06-Benchmark-Evidence.md).
- Reaches every engine metrics endpoint.

Other plans run on the workstation repository root through the private router tunnel.

1. Prepare the workload with the [load trial helper](03-Load-Trial.md#create-the-trial-directory-and-workload).
2. Write a private plan at `runs/benchmark-plan.json` with `workload.file` and `--workload` set to the prepared file:

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

`client_argv` is an argument vector that takes these placeholders:

| Placeholder   | Value                     |
| ------------- | ------------------------- |
| `{base}`      | The `--base` URL          |
| `{model}`     | The `--model` served name |
| `{point_id}`  | The point's `id`          |
| `{point_dir}` | The point's directory     |

Bearer-token ingress:

1. Export the token in an environment variable.
2. Pass the variable name with `--api-key-env NAME` for the runner's probes.
3. Give the external client separate credentials.

The new private `--out` directory holds:

| File                             | Location        | Contents                                                                                        |
| -------------------------------- | --------------- | ----------------------------------------------------------------------------------------------- |
| `manifest.json`                  | Output root     | Plan, its SHA-256 digest, runner digest, model, URL, and invocation                            |
| `result.json`                    | Point directory | Readiness, client exit status, initial and final drain condition, timestamps, last state snapshot |
| `client.stdout`, `client.stderr` | Point directory | External client output                                                                          |

The runner stops on:

- a refused `/ready` check
- a `/v1/models` model mismatch
- a nonzero client exit, after a drain attempt
- a router drain timeout before or after the client

For client exit `2` in the load trial helper's [exit codes](03-Load-Trial.md#8-measure-05-requests), read `summary.json` and `requests.jsonl`:

| `client_schedule_valid` in `summary.json` | Exit `2` means |
| --- | --- |
| `true` | A valid measured miss |
| `false` | A client that needs repair |
