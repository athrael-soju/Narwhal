# Upgrade, rollback, and release drills

## 10. Upgrade and rollback

### 10.1 Rolling upgrade with compatible handoff versions

First [check handoff compatibility](01-Start-Routers.md#2-keep-one-deployment-set)
between the installed and proposed releases. Record the active router's
`ha.epoch` from `/narwhal/state` so you can verify ownership after takeover.

1. Stop the standby.
2. Install the new release and its matching configuration, profiles, and
   deployment evidence on that host.
3. Start the upgraded router as standby.
4. Confirm its `/health` returns HTTP 200.
5. Confirm its `/ready` returns HTTP 503.
6. Stop the old active router cleanly.
7. Confirm the upgraded router's `ha.epoch` exceeds the recorded epoch.
8. Confirm it is the only backend returning HTTP 200 from `/ready`.
9. Install the same deployment set on the stopped router's host.
10. Start that router as standby and confirm its `/ready` returns HTTP 503.

### 10.2 Upgrade across a handoff-version change

Use a maintenance window when the releases have incompatible handoff versions.

1. Stop ingress.
2. Stop both routers.
3. Install the same release, configuration, profiles, and deployment evidence
   on both hosts.
4. Start the active router.
5. Start its standby.
6. Confirm the active router owns the lease and is the only backend returning
   HTTP 200 from `/ready`, then restore ingress.

### 10.3 Roll back

Stop the new router process before starting the previous build.

Restore these as one unit:

- code;
- configuration;
- profiles;
- handoff state with a schema version supported by the restored build.

## 11. Validate every release

On an idle fleet, run the restart drill for `recovery.engine_restart_policy`
and test router failover using the production supervisor and load balancer.
Keep external admission closed until
[service restoration](#restore-service-after-the-drill) is complete.

Record the release, fleet configuration, role pins, profiles, immutable engine
build, and engine and sidecar supervisor commands. Include restart policies,
resource limits, and log locations. Keep these records and drill results in
the private deployment directory. If an earlier run used another launcher,
test the production supervisor separately. Reuse earlier results where they
meet the pass conditions below; run the missing cases.

Readmission must reject missing or stale loaded profiles. Measure each
replacement and [load its fresh profiles while preserving the hold](03-Restart-Engines.md#activate-replacement-profiles)
before successful readmission. The `profile generation` check verifies the
loaded profiles against the running engine; the separate `generation` check
runs a direct completion.

### Release drill pass conditions

| Drill                     | Pass condition                                                                                                                                                                   |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Individual engine restart | Drain completes. Stale profiles block readmission; fresh profiles load with the hold preserved. Every check passes and a routed request uses the returned engine. |
| Whole-wave restart        | Readiness is withdrawn before stop. One failed member holds the whole wave; all members return together after validation. |
| Unplanned whole-wave hold | The detected failure excludes the fleet. Drain records current identities before every member is replaced and readmitted. |
| Router failover           | The load balancer selects one lease owner. Roles and cumulative counters survive takeover, and the previous primary remains fenced. |

### Individual restart drill

Use `recovery.engine_restart_policy = individual` and the prerequisites in
[Restart one engine](03-Restart-Engines.md#7-restart-one-engine).

1. Drain and replace the engine through the production supervisor. Retain the
   drain observation before the stop command, both process identities, and
   supervisor output.
2. Before activating fresh profiles, request readmission with the previous
   profile still loaded. Retain HTTP 409, the profile generation error, and
   `accepts_new = false`. For this expected failure, capture the response body
   without `curl --fail`.
3. Activate fresh profiles and request explicit readmission. Retain the
   successful response: `state = active`, `accepts_new = true`, a newer process
   start, and passing attestation, profile generation, model, generation,
   fabric, and final health checks.
4. Send a routed request and confirm its placement includes the returned
   engine. Match the client response and request ID to the
   [journal's terminal event](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal)
   and the router's cumulative counters. Retain state snapshots before and
   after the request to distinguish it from readmission probes.

### Whole-wave drill

Use `recovery.engine_restart_policy = whole_wave` and the prerequisites,
`ROUTER_URL`, and `RUN_DIR` setup in
[Engine restart and process replacement](03-Restart-Engines.md).

1. [Drain the wave](03-Restart-Engines.md#81-drain-the-wave). Retain the
   HTTP 503 readiness observation, every recorded drain identity, zero
   resident work, and `wave.ready_to_stop = true` before stopping any process.
2. Restart every engine and sidecar from the recorded build through its
   supervisor. While the wave remains held, [activate replacement
   profiles](03-Restart-Engines.md#activate-replacement-profiles) and verify
   that resume preserves every drain identity and the complete wave hold.
   Then stop one member's sidecar using the recorded supervisor command.
   Confirm that member's engine remains healthy. Keep the other sidecars
   running so readmission encounters one deliberate attestation failure.
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
   fails if it observes partial readmission, and exits after complete readmission.
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

5. Repair the failed member by starting its sidecar with the same attestation
   document for the replacement process. Confirm its attestation endpoint
   answers with the digest used by its new profile, then explicitly
   [request and verify wave readmission](03-Restart-Engines.md#82-restart-the-fleet).
   Repair alone must leave the wave held.
6. Verify that every member passed the newer process identity, attestation,
   profile generation, model, generation, role-permitted fabric transfer, and
   final health checks. Find the journal's `engine_lifecycle` event with
   `action = wave_readmitted`. Send a routed request and match its client
   result to the journal and router counters.

Save the samples, readmission response, and lifecycle journal events.

### Unplanned whole-wave hold drill

On a fleet with `recovery.engine_restart_policy = whole_wave`, wait until
every engine is admitted and idle, then stop one sidecar through the
production supervisor. The next successful engine health sweep detects the
unavailable attestation. Set `recovery.liveness_every` to a nonzero value before
the drill to enable detection on an idle fleet; see
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

1. Confirm the final profile store covers the serving fleet's current
   generations. Reuse the measurements already activated for successful
   readmission and retain the unchanged engines' evidence. A further process
   replacement requires fresh measurements and another activation.
2. If the drill used an isolated subset, assemble the complete serving
   fleet's store and run all [preflight gates](../deploy/06-Profile-and-Preflight.md#run-preflight)
   against the original fleet configuration with that store. Stop the
   temporary routers before starting the serving router.
3. Confirm the intended router owns the complete fleet and `/ready` returns
   HTTP 200. Send a routed request and match its client result to the journal
   and counters, then restore external admission.

A handoff for a subset cannot resume into a fleet with a different engine
set. Complete the subset's held readmission first. For a router replacement
with the same fleet, use [profile activation with resume](03-Restart-Engines.md#activate-replacement-profiles)
to preserve an outstanding hold. Preserve the original and drill journals
and state snapshots.
