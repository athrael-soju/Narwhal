# Synthetic load trial

## 7. Run the synthetic deployment trial

Use the [private tunnel](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation) from the management workstation.

The initial workload consists of:

```text
requests       = 200
input_tokens   = 8192
output_tokens  = 128
```

Test the workload first at:

```text
0.5 request/s
```

then at:

```text
1 request/s
```

A tested rate satisfies the candidate 95% target when at least 190 requests complete within both limits:

```text
TTFT <= 2.0 s
TPOT <= 0.0333 s
```

Before starting the trial:

1. verify that the workload lies inside the accepted profile domain;
2. verify that it fits inside the engine context limit;
3. retain launch records showing `--no-enable-prefix-caching`;
4. reserve the router for trial traffic so every client record can be reconciled with the router journal.

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

`prepare` queries the served model, sends an unscored 32-token completion using a fixed public seed prompt, and writes the returned token IDs to:

```text
workload/workload.json
```

For request sequence `n`, the sampler draws 8,192 input token IDs from that pool using:

```text
seed + n
```

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
| `2`   | The run completed with an attainment miss or client scheduling miss | Inspect `client_schedule_valid` in `summary.json`. Keep a correctly scheduled run containing latency misses or HTTP refusals as a valid rate measurement. If client start times were missed, inspect `requests.jsonl`, correct the client-side scheduling problem, and repeat the rate |
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

The client allows 64 concurrent requests by default.

Its scheduling-lag limit is:

```text
50 ms
```

Missing a scheduling slot creates a terminal client-miss record and invalidates the offered-rate comparison.

Every sent offer gets one attempt.

HTTP refusals, stream errors, and timeouts remain in the denominator.

Raise a client-side limit only after CPU, memory, network, and scheduling measurements show that the deployment client itself is the bottleneck.

Continue with [client and router reconciliation](04-Reconcile-and-Accept.md).
