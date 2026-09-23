# Engine restart and process replacement

## 7. Restart one engine

Use this procedure when:

```yaml
recovery.engine_restart_policy: individual
```

Narwhal must remove the engine from new placement before its supervisor changes the process.

### 7.1 Drain the engine

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/drain \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"],"deadline_s":300}'
```

Poll:

```text
/narwhal/lifecycle
```

Wait until:

```text
engines.e0.ready_to_stop = true
```

A drain deadline expiry leaves the engine excluded while preserving its resident work.

### 7.2 Replace the process

Restart the engine and attestation sidecar through their configured process manager.

Then verify:

1. the engine HTTP endpoints;
2. sidecar attestation against the new process identity.

### 7.3 Request readmission

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/readmit \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}'
```

Narwhal runs the readmission gates in this sequence:

1. health;
2. current process-bound attestation;
3. a process identity newer than the recorded drain identity;
4. configured model identity;
5. direct generation;
6. role-permitted KV transfer;
7. final health.

One failed gate keeps the engine blocked. The API returns HTTP 409 and identifies the failed gate.

### 7.4 Recover an unplanned ejection

Recovery from an unplanned breaker ejection runs the same validation sequence, using the current process identity.

A passing engine returns automatically.

A failed gate places the engine under operator control until repair and explicit readmission.

### 7.5 Recover loss of every placement peer

When a contracted fleet loses every placement peer, automatic recovery waits for the complete cohort and validates that cohort atomically.

One failed member keeps the cohort held.

Repair the failing check, then request whole-cohort readmission:

```text
POST /narwhal/lifecycle/readmit
{"wave":true}
```

If an individual lifecycle hold removes the last available peer, promote the hold to a wave:

```text
POST /narwhal/lifecycle/drain
{"wave":true}
```

After promoting the hold to a wave, follow [Restart an engine wave](#8-restart-an-engine-wave).

## 8. Restart an engine wave

Use wave lifecycle operations when:

```yaml
recovery.engine_restart_policy: whole_wave
```

This policy applies to engine builds that share peer state across the fleet. Drain and readmission operate on the complete wave.

A confirmed ejection, changed process identity, or failed identity verification places the whole fleet on hold and withdraws readiness until wave readmission completes.

### 8.1 Drain the wave

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/drain \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}'
```

Wait for both conditions:

- router readiness is withdrawn;
- `wave.ready_to_stop` becomes `true`.

### 8.2 Restart the fleet

Restart every engine and attestation sidecar from one immutable build.

Then request wave readmission:

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/readmit \
  -H 'content-type: application/json' \
  -d '{"wave":true}'
```

Narwhal returns the fleet in one state transition after:

1. every recorded drain identity has been superseded;
2. every validation gate has passed;
3. the role-permitted KV ring has completed.

One failed member keeps the complete wave excluded.

### 8.3 Recover an unplanned whole-wave hold

An unplanned whole-wave hold requires an explicit drain before process restart so Narwhal can record current process identities.

If identity collection reaches an engine that is already stopped:

1. start that engine while the fleet hold remains active;
2. retry the drain;
3. wait for `wave.ready_to_stop = true`;
4. restart every engine and sidecar, including any process started for identity collection;
5. request whole-wave readmission.

## 9. Detect process replacement

Contracted fleets verify process identity after each successful liveness sample.

Narwhal compares:

- `/version`;
- `process_start_time_seconds`;
- process-bound attestation;

against the accepted identity.

A difference triggers recovery.

The handoff stores:

- accepted process start values;
- restart policy.

Resume and standby takeover use that handoff state to validate each running engine before admission.

A version mismatch during contracted resume either places the fleet into a managed wave hold or causes startup to fail.

Automatic takeover requires:

```text
handoff schema version 1
```

A changed process start is detected on the next successful liveness sample, normally after:

```text
recovery.liveness_every * controller.monitor_interval_s
```

plus probe and monitoring time.

Whole-wave restart policy depends on these sweeps for process-replacement detection. Planned process changes therefore use the drain workflow, with accurate process-start metrics and attestation.
