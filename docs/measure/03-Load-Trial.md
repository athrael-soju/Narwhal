# Synthetic load trial

## 7. Run the synthetic deployment trial

The trial sends 200 requests, each with 8,192 input tokens and 128 output tokens, from the management workstation through the [private tunnel](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation). Start at 0.5 request/s. If that rate passes, let the router drain and try 1 request/s.

A rate passes when at least 190 of the 200 requests (95%) complete with TTFT at or below 2.0 s and TPOT at or below 0.0333 s. The helper reports this as candidate attainment.

Before you start, confirm that:

- the workload fits inside the accepted profile's domain and the engine's context limit
- the engine launch records show `--no-enable-prefix-caching`
- nothing else is using the router, so every client record can be matched to a journal entry

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

`prepare` asks the router which model it's serving, sends one unscored 32-token completion from a fixed public seed prompt, and saves the returned token IDs to `workload/workload.json` as a token pool. It uses the prompt's token IDs when the response includes them, and the generated ones otherwise. Request `n` draws its 8,192 input tokens from that pool using seed `seed + n`, so every run of the file sends the same prompts.

Both rates reuse this file, with these request settings:

```text
temperature        = 0
add_special_tokens = false
min_tokens         = 128
max_tokens         = 128
ignore_eos         = true
```

Each run's manifest records the workload digest, helper digest, workstation hostname, source revision, command, Python and httpx versions, and the limits in effect.

### What happens during a run

Before measuring, the helper waits until the router's admission queues are empty and there's no resident work. It sends one unscored warmup request with the full workload shape, then waits for the fleet to drain again. After that it schedules all 200 requests on a fixed clock, whether or not earlier requests have finished.

The client can fall behind too. By default it allows 64 concurrent requests and 50 ms of scheduling lag. When a request would exceed either limit, it's recorded as `client_schedule_miss` and the run is marked `client_schedule_valid: false`. Each request is sent once, and refusals, stream errors, and timeouts all stay in the 200-request denominator.

If you think the client limits are too tight, look at its CPU, memory, network, and scheduling measurements first to confirm it really is saturated.

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

Look through the retained records before you change the rate. The exit code tells you what to do next:

| Exit  | Meaning                                        | Next step                                                                 |
| ----- | ---------------------------------------------- | ------------------------------------------------------------------------- |
| `0`   | Schedule valid and target met                  | Let the router drain, then run the next rate                              |
| `1`   | Blocking error                                 | Diagnose the reported failure from the retained artifacts before retrying |
| `2`   | Target missed, or the client lost its schedule | Check `client_schedule_valid` (below)                                     |
| `130` | Interrupted; partial artifacts kept            | Inspect the partial record before retrying                                |

Exit `2` covers two different situations, so open `summary.json` and check `client_schedule_valid`:

- If it's `true`, the fleet missed the target at this rate. Keep the result and end the sweep.
- If it's `false`, the client couldn't keep to its schedule and the run doesn't tell you anything about the fleet. Inspect `requests.jsonl`, fix the client's scheduling, and repeat the rate.

## 9. Measure 1 request/s

Once the router has fully drained:

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
