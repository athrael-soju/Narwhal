# Synthetic load trial

## 7. Run the synthetic deployment trial

The trial sends 200 requests, each with 8,192 input tokens and 128 output tokens, from the management workstation through the [private tunnel](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation). Start at 0.5 request/s. If that rate passes, wait for the router to drain (admission queues empty, no resident work), then run 1 request/s.

A rate passes when at least 190 of the 200 requests (95%) complete with TTFT at or below 2.0 s and TPOT at or below 0.0333 s. The helper reports this fraction as candidate attainment.

Before starting, confirm that:

- The workload fits inside the accepted profile's domain and the engine's context limit.
- The engine launch records show `--no-enable-prefix-caching`.
- Nothing else is using the router, so every client record matches a journal entry.

### Create the trial directory and workload

Install the helper's dependencies and create a private run directory:

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

`prepare`:

1. Asks the router which model it is serving.
2. Sends one unscored 32-token completion from a fixed public seed prompt.
3. Saves the token IDs to `workload/workload.json` as a token pool. It uses the prompt's token IDs if the response includes them, otherwise the generated token IDs.

Request `n` draws its 8,192 input tokens from the pool using seed `seed + n`, so every run of the file sends the same prompts.

Both rates use this workload file and send these request parameters:

```text
temperature        = 0
add_special_tokens = false
min_tokens         = 128
max_tokens         = 128
ignore_eos         = true
```

Each run's manifest records the workload digest, helper digest, workstation hostname, source revision, command, Python and httpx versions, and the limits in effect.

### What happens during a run

Before measuring, the helper waits for the router to drain. It sends one unscored warmup request with the full workload shape, then waits for the router to drain again. After that it schedules all 200 requests on a fixed clock, whether or not earlier requests have finished.

The client allows 64 concurrent requests and 50 ms of scheduling lag by default. A request that would exceed either limit is recorded as `client_schedule_miss` and the run is marked `client_schedule_valid: false`. Each request is sent once, and refusals, stream errors, and timeouts all stay in the 200-request denominator.

Raise the client limits only after its CPU, memory, network, and scheduling measurements confirm the client host is saturated.

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

Look through the retained records before changing the rate, then read the exit code:

| Exit  | Meaning                                        | Next step                                                                 |
| ----- | ---------------------------------------------- | ------------------------------------------------------------------------- |
| `0`   | Schedule valid and target met                  | Wait for the router to drain, then run the next rate                      |
| `1`   | Blocking error                                 | Diagnose the reported failure from the retained artifacts before retrying |
| `2`   | Target missed, or the client lost its schedule | Check `client_schedule_valid` (below)                                     |
| `130` | Interrupted; partial artifacts kept            | Inspect the partial record before retrying                                |

For exit `2`, check `client_schedule_valid` in `summary.json`:

- `true`: the fleet missed the target at this rate. Keep the result and end the trial.
- `false`: the run is invalid and says nothing about the fleet. Inspect `requests.jsonl`, fix the client's scheduling, and repeat the rate.

## 9. Measure 1 request/s

After the router has drained:

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

Next: [reconcile the client and router records](04-Reconcile-and-Accept.md).
