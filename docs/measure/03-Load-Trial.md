# Synthetic load trial

## 7. Run the synthetic deployment trial

Through the [private tunnel](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation) from the management workstation, send 200 requests with 8,192 input tokens and 128 output tokens at 0.5 request/s. After a passing run and router drain, test 1 request/s. A rate meets the candidate 95% target when at least 190 requests complete with TTFT at or below 2.0 s and TPOT at or below 0.0333 s.

Before the trial, check that the workload fits the accepted profile domain and engine context limit, retain launch records showing `--no-enable-prefix-caching`, and reserve the router for trial traffic so each client record can be reconciled with the journal.

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

`prepare` queries the served model, sends an unscored 32-token completion from a fixed public seed prompt, and writes the returned token IDs to `workload/workload.json`. For request sequence `n`, the sampler draws 8,192 input IDs from that pool using `seed + n`.

Both rate tests reuse the same workload file with:

```text
temperature        = 0
add_special_tokens = false
min_tokens         = 128
max_tokens         = 128
ignore_eos         = true
```

Each run manifest records:

* workload digest;
* helper digest;
* workstation hostname;
* source revision;
* command;
* Python version;
* httpx version;
* selected limits.

## 8. Measure 0.5 request/s

Run:

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

Inspect the retained records before changing the offered rate.

Interpret the helper exit code as follows:

| Exit  | Meaning                                                             | Required action                                                                                                                                                                                                                                                                        |
| ----- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`   | Schedule validity and candidate attainment both passed              | Drain the router, then continue to the next rate                                                                                                                                                                                                                                       |
| `2`   | Candidate attainment or client scheduling missed                    | Inspect `client_schedule_valid` in `summary.json`. A scheduled run with latency misses or HTTP refusals measures that rate. For missed client starts, inspect `requests.jsonl`, repair scheduling, and repeat the rate                                                     |
| `1`   | The helper reported a blocking error                                | Diagnose the reported failure and retained artefacts before retrying                                                                                                                                                                                                                   |
| `130` | The run was interrupted and partial artefacts were preserved        | Inspect the partial record before retrying                                                                                                                                                                                                                                             |

## 9. Measure 1 request/s

After the router has fully drained:

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

Before each measured run, the helper:

1. waits for empty router admission queues and zero resident work;
2. sends one unscored warmup request using the complete workload shape;
3. drains the fleet again;
4. schedules all 200 offers independently of response completion.

The helper allows 64 concurrent requests and 50 ms scheduling lag by default; exceeding either limit records a terminal `client_schedule_miss` and marks `client_schedule_valid: false`. The client sends each offer once and keeps HTTP refusals, stream errors, and timeouts in the 200-offer denominator.

Use CPU, memory, network, and scheduling measurements to establish client saturation before raising its limits.

Continue with [client and router reconciliation](04-Reconcile-and-Accept.md).
