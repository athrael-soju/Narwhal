# Ordered benchmark points

`tools/measurement/benchmark_runner.py` runs the plan's points in order, with these steps per point:

1. Check that `/ready` reports `ready`.
2. Check that `/v1/models` lists exactly the `--model` name.
3. Wait for an idle router.
4. Start the [evidence collector](06-Benchmark-Evidence.md) when the plan has an `evidence` object.
5. Run the point's client.
6. Wait for an idle router.
7. Write `result.json`.

Prerequisites:

- A passing preflight against the current fleet.
- A router reserved for this run.
- A router and engines that stay running through the benchmark.

| Plan | Runner host |
| --- | --- |
| With an `evidence` object | A host that reads the router's append-only JSONL journal as a local file and reaches the router and every engine metrics endpoint |
| Other plans | The workstation repository root, through the private router tunnel |

## Run a plan

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

## Plan fields

| Field | Value |
| --- | --- |
| `schema` | `1` |
| `points` | Ordered array of points |
| `evidence` | Optional [evidence collector](06-Benchmark-Evidence.md) settings |
| `points[].id` | Unique name of letters, digits, hyphens, and underscores |
| `points[].workload` | Nonempty object copied into `result.json` |
| `points[].client_argv` | Nonempty argument vector that contains `{base}` and `{model}` |
| `points[].client_timeout_s` | Positive client deadline in seconds |
| `points[].drain_timeout_s` | Positive drain deadline in seconds for each drain wait |

`client_argv` placeholders:

| Placeholder   | Value                     |
| ------------- | ------------------------- |
| `{base}`      | The `--base` URL          |
| `{model}`     | The `--model` served name |
| `{point_id}`  | The point's `id`          |
| `{point_dir}` | The point's directory     |

Bearer-token ingress:

1. Export the token in an environment variable.
2. Pass the variable name with `--api-key-env NAME` for the runner's probes, drain checks, and router samples.
3. Pass client credentials in `client_argv`, such as the load trial helper's `--api-key-env NAME`.

## Output and exit status

The new private `--out` directory holds:

| File                             | Location        | Contents                                                                                        |
| -------------------------------- | --------------- | ----------------------------------------------------------------------------------------------- |
| `manifest.json`                  | Output root     | Plan, its SHA-256 digest, runner digest, model, URL, and invocation                             |
| `result.json`                    | Point directory | Readiness, client exit status, initial and final drain condition, timestamps, last state snapshot, and evidence diagnostic count |
| `client.stdout`, `client.stderr` | Point directory | External client output                                                                          |

| Runner exit | Condition |
| :---: | --- |
| `0` | Every point reaches `completed` |
| `1` | The plan fails validation, or a point ends with another `condition` |

The runner stops at the first point that ends with one of these `result.json` conditions:

| `condition` | Cause |
| --- | --- |
| `readiness_refused` | `/ready` returned a status other than `200` with `ready` |
| `models_unavailable` | `/v1/models` returned a status other than `200` |
| `model_mismatch` | `/v1/models` listed models other than exactly `--model` |
| `probe_error` | A readiness or model probe failed |
| `initial_drain_timeout`, `initial_drain_state_error` | The drain wait before the client timed out or failed to read `/narwhal/state` |
| `drain_timeout`, `drain_state_error` | The drain wait after the client timed out or failed to read `/narwhal/state` |
| `client_failure` | The client timed out, failed to start, or returned a nonzero [exit code](03-Load-Trial.md#8-measure-05-requests) |
| `evidence_error` | The evidence collector failed |
