---
description: Run a synthetic load trial at 0.5 and 1 request/s against a frozen Narwhal deployment.
---

# Synthetic load trial

## 7. Running the synthetic deployment trial

1. Send 200 requests with 8,192 input tokens and 128 output tokens at 0.5 request/s from the management workstation, through the [router tunnel](../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation).
2. Confirm that the 0.5 request/s rate passes.
3. Wait for the router to drain.
4. Send the same 200 requests at 1 request/s.

A rate passes when:

- the client schedule is valid
- at least 190 of 200 requests meet both limits:
    - time to first token (TTFT) at or below 2.0 s
    - time per output token (TPOT) at or below 0.0333 s

HTTP refusals, stream errors, timeouts, and scheduling misses count in the 200-offer denominator.

### Prerequisites

- The workload fits the accepted profile domain.
- The workload fits the engine context limit.
- All router traffic is trial traffic.
- Each engine is [launched](../deploy/03-Validate-Engines.md#prepare-check-and-start-each-engine) with `--no-enable-prefix-caching` in `runtime.extra_args`.
- `checked.json` shows `"prefix_caching": false`.

### Creating the trial directory and workload

```bash
make setup

export NARWHAL_TRIAL_URL=http://127.0.0.1:18000

umask 077
mkdir -p runs

TRIAL_DIR=$(mktemp -d "$PWD/runs/load-trial-XXXXXXXX")

.venv/bin/python tools/measurement/load_trial.py prepare \
  --base "$NARWHAL_TRIAL_URL" \
  --input-tokens 8192 \
  --output-tokens 128 \
  --seed 1729 \
  --out "$TRIAL_DIR/workload"
```

`prepare` writes `seed-response.json` and `workload.json` under `workload/`:

| Item              | Value                                                                                                           |
| ----------------- | --------------------------------------------------------------------------------------------------------------- |
| Model             | The single served model from the router's `/v1/models`                                                          |
| Seed request      | One unscored 32-token completion from a fixed public seed prompt                                                |
| Token pool        | The returned prompt token IDs when they contain two or more distinct IDs, otherwise the 32 generated output IDs |
| Preparation error | Fewer than two distinct IDs in the pool                                                                         |
| Request `n`       | 8,192 input IDs drawn from the pool with `seed + n`                                                             |

Request parameters for both rates:

```text
temperature        = 0
add_special_tokens = false
min_tokens         = 128
max_tokens         = 128
ignore_eos         = true
```

Each run manifest records:

- workload digest
- helper digest
- source revision
- command
- Python version
- httpx version
- every command-line setting, including the client limits

| Client limit        | Flag             | Default |
| ------------------- | ---------------- | :-----: |
| Concurrent requests | `--max-inflight` | 64      |
| Scheduling lag      | `--max-lag`      | 0.05 s  |

An offer over either limit becomes a terminal `client_schedule_miss` record with `client_schedule_valid: false` in the summary.

Check client CPU and scheduling lag before raising either limit.

## 8. Measuring 0.5 request/s

```bash
.venv/bin/python tools/measurement/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" \
  --workload "$TRIAL_DIR/workload/workload.json" \
  --rate 0.5 \
  --requests 200 \
  --ttft 2 \
  --tpot 0.0333 \
  --attainment 0.95 \
  --out "$TRIAL_DIR/rate-0.5"
```

| Exit  | Meaning                                                | Required action                                          |
| :---: | ------------------------------------------------------ | -------------------------------------------------------- |
| `0`   | Schedule validity and candidate attainment both passed | Drain the router                                         |
| `1`   | Blocking error                                         | Check the error and the run directory                    |
| `2`   | Candidate attainment or client scheduling missed       | Inspect `client_schedule_valid` in `summary.json`        |
| `130` | Interrupted run with partial artifacts                 | Keep the partial output and rerun into a new `--out` path |

For exit code `2`:

| `client_schedule_valid` | Meaning                               | Required action                                                                   |
| ----------------------- | ------------------------------------- | --------------------------------------------------------------------------------- |
| `true`                  | The rate missed the attainment target | Keep the result and stop                                                          |
| `false`                 | Client scheduling missed              | 1. Inspect `requests.jsonl`.<br>2. Repair client scheduling.<br>3. Repeat the rate. |

## 9. Measuring 1 request/s

The router has drained when `/narwhal/state` reports zero for:

- the `inflight`, `queued`, `waiting_prefill`, and `waiting_decode` admission counts
- `serving.http_retained`
- resident prefill and decode work on every engine

A drain wait longer than `--timeout` (default 120 s) exits `1` with a drain-deadline error.

Run the second rate:

```bash
.venv/bin/python tools/measurement/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" \
  --workload "$TRIAL_DIR/workload/workload.json" \
  --rate 1 \
  --requests 200 \
  --ttft 2 \
  --tpot 0.0333 \
  --attainment 0.95 \
  --out "$TRIAL_DIR/rate-1"
```
