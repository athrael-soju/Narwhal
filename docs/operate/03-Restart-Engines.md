# Engine restart and process replacement

Run these commands from a management host with `curl`, Python 3, and access to
the router's private control endpoints. The fleet must have a complete
[`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract).

Replace `http://router:8000` with your router URL and `e0` with the engine ID
where an individual engine is named. Keep the same shell for the commands in
your chosen procedure. Save observations in a new private run directory:

```bash
ROUTER_URL='http://router:8000'
RUN_DIR="runs/restart-$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$RUN_DIR"
```

Before draining, record the deployment's engine and sidecar stop/start
commands, process or container identities, immutable build, restart policy,
resource limits, and log locations. Run those commands through the configured
process manager when the procedure reaches process replacement. Narwhal
records permission to stop a process; it does not stop or launch that process.

Keep external admission closed during replacement and drill validation.
Current lifecycle readmission omits saved-profile generation binding
([#160](https://github.com/athrael-soju/Narwhal/issues/160)). Its `generation`
check is a direct completion probe. Startup and preflight reject profiles
bound to a previous engine process generation.

After the lifecycle checks, measure fresh profiles for the replacement
processes, run all preflight gates, and restart the serving router to load the
final profile store before reopening external admission. Updating the profile
files does not reload the running router's in-memory `ProfileStore`. Follow
[Restore service after the drill](04-Upgrade-and-Validate.md#restore-service-after-the-drill)
for the activation sequence.

## 7. Restart one engine

Use this procedure when:

```yaml
recovery.engine_restart_policy: individual
```

Narwhal must remove the engine from new placement before its supervisor changes the process.

### 7.1 Drain the engine

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"],"deadline_s":300}' \
  > "$RUN_DIR/individual-drain-response.json"
```

Repeat this observation until it prints `e0 ready to stop`. The engine must
have no resident prefill or decode work, reject new placement, and have a
recorded process identity before its supervisor stops it:

```bash
curl -fsS "$ROUTER_URL/narwhal/lifecycle" \
  > "$RUN_DIR/individual-drained.json"
python3 - "$RUN_DIR/individual-drained.json" <<'PY'
import json
import sys

engine = json.load(open(sys.argv[1]))["engines"]["e0"]
assert engine["ready_to_stop"], engine
assert not engine["accepts_new"], engine
assert engine["resident"] == {"prefill": 0, "decode": 0}, engine
assert engine["old_process_start"] is not None, engine
print("e0 ready to stop")
PY
```

A drain deadline expiry leaves the engine excluded while preserving its resident work.

### 7.2 Replace the process

Restart the engine and attestation sidecar through their configured process manager.

Then verify:

1. the engine `/health` endpoint returns HTTP 200;
2. `/version`, `/metrics`, and `/v1/models` describe the replacement process
   and configured model;
3. the restarted sidecar serves attestation bound to that process.

Retain the process manager's stop/start output and the new engine and sidecar
identities. A successful `/health` response can have an empty body; use its
HTTP status to check health.

### 7.3 Request readmission

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}' \
  > "$RUN_DIR/individual-readmitted.json"
```

Narwhal validates the candidate and its role-permitted peers before returning
the candidate to placement:

1. For each participant, check health, process identity, process-bound
   attestation, and the configured model. A peer must retain its accepted
   process identity.
2. For each candidate, require a process start newer than its recorded drain
   identity and run direct generation.
3. If those checks pass, exercise the role-permitted KV transfers. Recheck
   participant identities and attestation before each transfer and after its
   prefill leg.
4. Check candidate health again, then recheck all participant identities and
   attestation.

A failed validation returns HTTP 409 and keeps the candidate blocked. The
response's `engines.<id>.error` records the failure; `checks` lists the checks
that passed. Some checks continue after another check fails, so a nonempty
`checks` list alone does not establish readmission.

Verify the successful response and retain its checks:

```bash
python3 - "$RUN_DIR/individual-readmitted.json" <<'PY'
import json
import sys

engine = json.load(open(sys.argv[1]))["engines"]["e0"]
assert engine["state"] == "active" and engine["accepts_new"], engine
assert engine["new_process_start"] > engine["old_process_start"], engine
assert {"health", "model", "new process identity", "generation", "final health"} <= set(engine["checks"]), engine
assert any(check.startswith("attestation ") for check in engine["checks"]), engine
print("e0 readmitted:", engine["checks"])
PY
```

Confirm the reported `fabric produce to ...` and `fabric consume from ...`
checks cover the roles permitted by the engine's pins. An unpinned candidate
must pass both directions against eligible peers. Then send a routed request
and verify its engine placement and terminal outcome in the
[request journal](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).

### 7.4 Recover an unplanned ejection

Recovery from an unplanned breaker ejection runs the same validation sequence, using the current process identity.

A passing engine returns automatically.

A failed gate places the engine under operator control until repair and explicit readmission.

### 7.5 Recover loss of every placement peer

When a fleet with an [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) loses every placement peer, automatic recovery waits for all configured engines to pass their health probes, then validates them atomically.

One failed member keeps the fleet held.

Repair the failing check, then request whole-fleet readmission:

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
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}' \
  > "$RUN_DIR/wave-drain-response.json"
```

Repeat this observation until it prints `wave ready to stop`. Save this
observation before issuing the first supervisor stop command:

```bash
curl -fsS "$ROUTER_URL/narwhal/lifecycle" > "$RUN_DIR/wave-drained.json"
curl -sS -o "$RUN_DIR/wave-ready-body.txt" -w '%{http_code}\n' \
  "$ROUTER_URL/ready" > "$RUN_DIR/wave-ready-status.txt"
python3 - "$RUN_DIR/wave-drained.json" "$RUN_DIR/wave-ready-status.txt" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert open(sys.argv[2]).read().strip() == "503"
assert state["wave"]["active"] and state["wave"]["ready_to_stop"], state
assert not state["router"]["ready"], state
for iid, engine in state["engines"].items():
    assert engine["ready_to_stop"] and not engine["accepts_new"], (iid, engine)
    assert engine["resident"] == {"prefill": 0, "decode": 0}, (iid, engine)
    assert engine["old_process_start"] is not None, (iid, engine)
print("wave ready to stop:", state["wave"]["id"])
PY
```

### 8.2 Restart the fleet

Restart every engine and attestation sidecar from one immutable build using
the recorded process manager commands. Retain the old and new identities and
repeat the endpoint checks in [Replace the process](#72-replace-the-process)
for every member.

Then request wave readmission:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"wave":true}' > "$RUN_DIR/wave-readmitted.json"
```

Narwhal returns the fleet in one state transition after:

1. every recorded drain identity has been superseded;
2. every validation gate has passed;
3. the role-permitted KV ring has completed.

One failed member keeps the complete wave excluded.

Verify the completed wave:

```bash
python3 - "$RUN_DIR/wave-readmitted.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert not state["wave"]["active"] and state["router"]["ready"], state
for iid, engine in state["engines"].items():
    assert engine["state"] == "active" and engine["accepts_new"], (iid, engine)
    assert engine["new_process_start"] > engine["old_process_start"], (iid, engine)
    assert {"health", "model", "new process identity", "generation", "final health"} <= set(engine["checks"]), (iid, engine)
    assert any(check.startswith("attestation ") for check in engine["checks"]), (iid, engine)
    print(iid, engine["checks"])
PY
curl -fsS -o /dev/null "$ROUTER_URL/ready"
```

Check the recorded fabric directions against the configured role pins. Two
unpinned engines require both directed transfers. A fleet pinned to one
prefill engine and one decode engine requires the prefill-to-decode transfer.

If readmission fails, inspect each member's `error`, repair the failing gate,
and repeat the explicit wave readmission request. Repair alone does not
release a wave hold. The [whole-wave release drill](04-Upgrade-and-Validate.md#whole-wave-drill)
records a deliberate member failure and observations during readmission.

### 8.3 Recover an unplanned whole-wave hold

An unplanned whole-wave hold requires an explicit drain before process restart
so Narwhal can record current process identities. Record the initial hold:

```bash
curl -fsS "$ROUTER_URL/narwhal/lifecycle" > "$RUN_DIR/unplanned-hold.json"
python3 - "$RUN_DIR/unplanned-hold.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert state["wave"]["active"] and not state["router"]["ready"], state
assert all(not engine["accepts_new"] for engine in state["engines"].values()), state
for iid, engine in state["engines"].items():
    print(iid, engine["old_process_start"], engine["error"])
PY
```

Request drain and retain the response even when identity collection fails:

```bash
curl -sS -o "$RUN_DIR/unplanned-drain.json" -w '%{http_code}\n' \
  -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}' \
  > "$RUN_DIR/unplanned-drain-status.txt"
python3 - "$RUN_DIR/unplanned-drain.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
print("drain error:", state["error"])
for iid, engine in state["engines"].items():
    print(iid, engine["old_process_start"], engine["error"])
PY
```

An unreadable engine identity returns HTTP 503 and leaves that member's
`old_process_start` unset. The whole fleet remains excluded. If identity
collection reaches an engine that is already stopped:

1. Start that engine through its process manager while the fleet hold remains
   active. Wait for its process identity endpoints to answer.
2. Retry [Drain the wave](#81-drain-the-wave). Narwhal retains identities
   already captured and collects the missing ones.
3. Run the drain observation and wait for `wave.ready_to_stop = true` before
   stopping any process.
4. [Restart the fleet](#82-restart-the-fleet), including every process started
   for identity collection, and request whole-wave readmission.

If every identity was readable on the first drain, proceed from its successful
drain observation to fleet restart. Do not use the accepted identities in
`process_starts` as substitutes for the required `old_process_start` records.

## 9. Detect process replacement

After each successful liveness sample, Narwhal compares the running engine's `/version`, `process_start_time_seconds`, and process-bound attestation with its accepted identity, starting recovery when a value changes.

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
