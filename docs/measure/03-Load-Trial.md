# Synthetic load trial

## 7. Run the synthetic deployment trial

Send 200 requests with 8,192 input tokens and 128 output tokens at 0.5 request/s from the management workstation, through the [router tunnel](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation).

After the 0.5 request/s run passes and the router drains, test 1 request/s.

A rate passes when the client schedule is valid and at least 190 of 200 requests meet both limits:

- time to first token (TTFT) at or below 2.0 s
- time per output token (TPOT) at or below 0.0333 s

### Prerequisites

- The workload fits the accepted profile domain and the engine context limit.
- Nothing else sends traffic to the router.
- Both rates replay one workload to measure cold prefill, so prefix caching must be off; see [Prepare, check, and start each engine](../deploy/03-Validate-Engines.md#prepare-check-and-start-each-engine). Launch every engine with `--no-enable-prefix-caching` in `runtime.extra_args`. Confirm `checked.json` shows `"prefix_caching": false`.

### Create the trial directory and workload

Install the helper dependencies and create a private run directory:

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

`prepare` writes a token pool to `workload/workload.json`:

| Item           | Value                                                                                 |
| -------------- | ------------------------------------------------------------------------------------- |
| Seed request   | One unscored 32-token completion from a fixed public seed prompt |
| Token pool     | The returned prompt token IDs when they contain two or more distinct IDs, otherwise the 32 generated output IDs |
| Stop condition | Fewer than two distinct IDs in the pool |
| Request `n`    | 8,192 input IDs drawn from the pool with `seed + n` |

Both rates reuse the workload with:

```text
temperature        = 0
add_special_tokens = false
min_tokens         = 128
max_tokens         = 128
ignore_eos         = true
```

Each run manifest records the workload and helper digests, source revision, command, Python and httpx versions, and limits.

Before each measured run, the helper drains the router, sends one unscored full-shape warmup, and drains again. Offers go out on a fixed schedule, whatever the responses do.

| Client limit        | Default |
| ------------------- | ------- |
| Concurrent requests | 64      |
| Scheduling lag      | 50 ms   |

An offer over either limit records a terminal `client_schedule_miss` and sets `client_schedule_valid: false`.

The helper sends each offer once. HTTP refusals, stream errors, and timeouts stay in the 200-offer denominator.

Check client CPU and scheduling lag before raising these limits.

## 8. Measure 0.5 request/s

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

Check the exit code before changing the rate:

| Exit  | Meaning                                                | Required action                                                      |
| ----- | ------------------------------------------------------ | -------------------------------------------------------------------- |
| `0`   | Schedule validity and candidate attainment both passed | Drain the router and continue to the next rate                       |
| `1`   | The helper reported a blocking error                   | Read the error, then check the run directory |
| `2`   | Candidate attainment or client scheduling missed       | Inspect `client_schedule_valid` in `summary.json`                    |
| `130` | Interrupted run with partial artifacts                 | Keep the partial output, then rerun                           |

For exit code `2`:

- `client_schedule_valid` is `true`: the rate missed the target. Keep the result and stop.
- `client_schedule_valid` is `false`: inspect `requests.jsonl`, repair client scheduling, and repeat the rate.

## 9. Measure 1 request/s

The router has drained when `/narwhal/state` reports zero for:

- the `inflight`, `queued`, `waiting_prefill`, and `waiting_decode` admission counts
- `serving.http_retained`
- resident prefill and decode work on every engine

The helper waits for zero after each run and before warmup, and fails with a drain-deadline error if `--timeout` runs out.

After the router drains, run the second rate:

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

Continue with [client and router reconciliation](04-Reconcile-and-Accept.md).
