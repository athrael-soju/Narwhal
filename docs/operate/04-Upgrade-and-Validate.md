# Upgrade, rollback, and release drills

## 10. Upgrade and rollback

### 10.1 Rolling upgrade with compatible handoff versions

1. Stop the standby.
2. Install the new release and matching deployment set on that host.
3. Start it as standby.
4. Confirm `/health` returns HTTP 200.
5. Confirm `/ready` returns HTTP 503.
6. Stop the old active router cleanly.
7. Confirm the upgraded router owns a higher lease epoch.
8. Confirm the upgraded router is the only ready backend.
9. Upgrade the stopped router.
10. Return it as standby.

### 10.2 Upgrade across a handoff-version change

Use a maintenance window.

1. Stop ingress.
2. Stop both routers.
3. Install one coherent deployment set on both hosts.
4. Start the active router.
5. Start its standby.
6. Restore ingress after admission state has been verified.

### 10.3 Roll back

Stop the new process before the previous build can claim the fleet.

Restore these as one unit:

- code;
- configuration;
- profiles;
- a state version supported by the restored build.

## 11. Validate every release

Before production admission, run the permitted engine-restart procedure and router failover on an idle fleet using the production supervisor and load balancer. Confirm the conditions below against the running engine processes and router lease.

Record the release, fleet configuration, role pins, profiles, immutable engine
build, and the exact engine and sidecar supervisor commands in the private
deployment record. Include the supervisor's restart policy, resource limits,
and log locations. A replacement performed with a different launcher
qualifies that launch procedure; record production-supervisor validation
separately until its commands have been exercised.

When reusing an earlier run, map its retained observations to the pass
conditions below and run the missing cases. Keep deployment addresses,
commands, identities, journals, and results in the private record.

Keep external admission closed through the drill and
[profile activation](#restore-service-after-the-drill). Lifecycle readmission
currently omits saved-profile generation binding
([#160](https://github.com/athrael-soju/Narwhal/issues/160)); its `generation`
check runs a direct completion. A successful lifecycle response and readiness
HTTP 200 therefore do not establish that the running router has profiles for
the replacement processes.

### Release drill pass conditions

| Drill                     | Pass condition                                                                                                                                                                   |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Individual engine restart | Placement stops after drain, resident work reaches zero, every readmission gate passes, and a routed request uses the returned engine with a reconciled terminal outcome                                         |
| Whole-wave restart        | Readiness is withdrawn before stop; one failed member holds the complete wave; every replacement passes attestation and the role-permitted KV ring before atomic readmission |
| Unplanned whole-wave hold | The fleet is excluded after the detected failure, explicit drain captures current identities, and every member is replaced and validated before readmission                  |
| Router failover           | The load balancer selects one lease owner, roles and cumulative counters survive, and the previous primary remains fenced                                                        |

### Individual restart drill

Follow [Restart one engine](03-Restart-Engines.md#7-restart-one-engine) using
the production supervisor. Retain the drain observation before the stop
command, both process identities, supervisor output, and the successful
readmission response. The response must show `state = active`,
`accepts_new = true`, a newer process start, and the passing attestation,
model, generation, fabric, and final-health checks.

Send a routed request after readmission and confirm its placement includes
the returned engine. Reconcile the client response and request ID with the
[journal's terminal event](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal)
and the router's cumulative counters. Retain the before/after state snapshots
so the readmission probes are distinguishable from routed requests.

### Whole-wave drill

Use the `ROUTER_URL` and `RUN_DIR` setup in
[Engine restart and process replacement](03-Restart-Engines.md).

1. [Drain the wave](03-Restart-Engines.md#81-drain-the-wave). Retain the
   HTTP 503 readiness observation, every recorded drain identity, zero
   resident work, and `wave.ready_to_stop = true` before stopping any process.
2. Restart every engine from the recorded build through its supervisor.
   Restart the sidecars, then stop one member's sidecar using the recorded
   supervisor command. Confirm that member's engine remains healthy. Keep the
   other sidecars running so readmission encounters one deliberate
   attestation failure.
3. Request readmission and retain its failure response:

    ```bash
    curl -sS -o "$RUN_DIR/wave-failed.json" -w '%{http_code}\n' \
      -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
      -H 'content-type: application/json' -d '{"wave":true}' \
      > "$RUN_DIR/wave-failed-status.txt"
    curl -sS -o /dev/null -w '%{http_code}\n' "$ROUTER_URL/ready" \
      > "$RUN_DIR/wave-failed-ready-status.txt"
    python3 - "$RUN_DIR/wave-failed.json" \
      "$RUN_DIR/wave-failed-status.txt" "$RUN_DIR/wave-failed-ready-status.txt" <<'PY'
    import json
    import sys

    state = json.load(open(sys.argv[1]))
    assert open(sys.argv[2]).read().strip() == "409"
    assert open(sys.argv[3]).read().strip() == "503"
    assert state["wave"]["active"] and not state["router"]["ready"], state
    for iid, engine in state["engines"].items():
        assert engine["state"] == "blocked" and not engine["accepts_new"], (iid, engine)
        print(iid, engine["error"], engine["checks"])
    PY
    ```

    Confirm the stopped sidecar caused its member's attestation failure.
    Each other member must report that another engine in the wave failed
    validation. Investigate any additional failure before continuing.

4. In a second terminal, set the same `ROUTER_URL` and `RUN_DIR` values and
   start this sampler while the wave is held. It saves each observation,
   rejects a partially admitted wave, and exits after complete readmission.
   Press Ctrl+C to stop it if the repair or readmission cannot proceed.

    ```bash
    python3 - "$ROUTER_URL" "$RUN_DIR/wave-samples.jsonl" <<'PY'
    import json
    import sys
    import time
    import urllib.request

    url = sys.argv[1].rstrip("/") + "/narwhal/lifecycle"
    with open(sys.argv[2], "x") as output:
        while True:
            with urllib.request.urlopen(url, timeout=10) as response:
                state = json.load(response)
            output.write(json.dumps({"at": time.time(), "lifecycle": state}) + "\n")
            output.flush()
            engines = state["engines"]
            accepting = {iid for iid, engine in engines.items() if engine["accepts_new"]}
            assert not accepting or accepting == set(engines), state
            if not state["wave"]["active"]:
                assert accepting == set(engines) and state["router"]["ready"], state
                assert all(engine["state"] == "active" for engine in engines.values()), state
                print("complete wave readmitted")
                break
            assert not accepting and not state["router"]["ready"], state
            time.sleep(0.25)
    PY
    ```

5. Repair the failed member by starting its sidecar against the replacement
   process. Confirm its attestation endpoint answers, then explicitly
   [request and verify wave readmission](03-Restart-Engines.md#82-restart-the-fleet).
   Repair alone must leave the wave held.
6. Inspect the retained checks for every member's newer process identity,
   attestation, model, generation, role-permitted fabric transfers, and final
   health. Check the journal's `engine_lifecycle` events for the completed
   `wave_readmitted` event. Send a routed request and reconcile its terminal
   outcome with the journal and router counters.

The sampler establishes the states observed during the drill. Retain its
samples alongside the readmission response and lifecycle journal events.

### Unplanned whole-wave hold drill

With the fleet admitted and idle, stop one sidecar through the
production supervisor. A successful engine health sweep then encounters
unverifiable attestation. `recovery.liveness_every` must be nonzero for this
idle-fleet detection path; see
[Detect process replacement](03-Restart-Engines.md#9-detect-process-replacement)
for the sampling interval.

Wait for readiness withdrawal and exclusion of every member. Retain the
`process_excluded` and `restart_required` lifecycle events, then follow
[Recover an unplanned whole-wave hold](03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold).
Confirm drain captures identities before every engine and sidecar is replaced,
then verify complete readmission and a routed request.

To qualify the already-stopped-engine branch of that procedure, stop an
engine rather than its sidecar. Retain the failed drain response with its
missing identity, the temporary process start, and the successful drain retry.
The temporary process must also be replaced after the drain. Record the
tested trigger and branch with the results.

### Restore service after the drill

Keep external admission closed until these steps complete:

1. Keep the measured engines idle and
   [measure fresh profiles](../deploy/06-Profile-and-Preflight.md#profile-idle-engines)
   for the replacement processes. Retain profiles and measurement evidence for
   engines whose generations are unchanged.
2. Run all [preflight gates](../deploy/06-Profile-and-Preflight.md#run-preflight)
   against the final fleet configuration and profile store.
3. Stop and restart the serving router with that profile store. Startup
   validates its profiles against the live engine generations. Updating the
   profile files alone does not reload the running router's in-memory
   `ProfileStore`.
4. Confirm the intended router owns the complete fleet, `/ready` returns
   HTTP 200, and a routed request has a reconciled terminal outcome. Then
   restore external admission.

If the drill used an isolated fleet subset, stop its temporary routers before
starting the original fleet configuration. A handoff for a subset cannot
resume into a fleet with a different engine set. Preserve the original and
drill journals and state snapshots.
