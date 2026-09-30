# Upgrade, rollback, and release drills

## 10. Upgrade and rollback

### 10.1 Rolling upgrade with compatible handoff versions

1. Check that both releases share a [handoff version](01-Start-Routers.md#2-keep-one-deployment-set).
2. Record the active router's `ha.epoch` from `/narwhal/state`.
3. Stop the standby.
4. Install the new deployment set on that host.
5. Start the upgraded router as standby.
6. Check that `/health` returns HTTP 200 and `/ready` returns HTTP 503 on the new standby.
7. Stop the old active router gracefully.
8. Verify the upgraded router's `ha.epoch` exceeds the epoch recorded in step 2.
9. Check that it is the only backend returning HTTP 200 from `/ready`.
10. Install the same deployment set on the stopped router's host.
11. Start that router as standby.
12. Wait for its `/ready` to return HTTP 503.

### 10.2 Upgrade across a handoff-version change

Incompatible handoff versions need a maintenance window.

1. Stop ingress.
2. Stop both routers.
3. Install the same deployment set on both hosts.
4. Start the intended primary.
5. Start its standby.
6. Confirm the primary holds the lease and is the only backend returning HTTP 200 from `/ready`.
7. Restore ingress.

### 10.3 Roll back

1. Remove the router you are rolling back from the load balancer.
2. Stop the new router gracefully.
3. Inspect the rollback build's contract support:

    ```bash
    narwhal-check --print-contract-versions
    ```

4. Restore these as one unit:
    - code;
    - configuration;
    - profiles;
    - the first-token calibration artifact, matching the live process generations;
    - a state handoff the restored build supports.
5. If process generations changed, regenerate the profiles and [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) before preflight.
6. For fresh roles and zeroed counters:
    - set `recovery.resume: false`;
    - drop `--resume`.
7. Confirm exactly one router holds the lease, either the rollback router or its fenced peer.
8. Start the rollback build and check:
    - `/health` reports `status: ok`;
    - `/ready` returns HTTP 200;
    - roles;
    - cumulative counters;
    - one completion request.
9. Return the router to service when the checks pass.
10. Restore its standby.

## 11. Validate every release

Run the release drills on an idle fleet with external admission closed:

1. Run the restart drill for your `recovery.engine_restart_policy`.
2. Test [router failover](../troubleshoot/02-Router-Recovery.md#router-failover) through the production supervisor and load balancer.
3. [Restore service](#restore-service-after-the-drill).

Keep the release, fleet configuration, profiles, engine build, supervisor commands, and drill results in the private deployment directory.

For earlier drill results:

- reuse results that meet the pass conditions;
- test the production supervisor when an earlier run used another launcher.

### Release drill pass conditions

| Drill                     | Pass condition                                                                                                                                                                   |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Individual engine restart | The drain completes, stale profiles block readmission, fresh profiles load with the hold preserved, and a routed request reaches the returned engine. |
| Whole-wave restart        | The router withdraws readiness before the first stop, one failed member holds the whole wave, and every member returns together after validation. |
| Unplanned whole-wave hold | The drain records current identities for the whole excluded wave before member replacement and readmission. |
| Router failover           | The load balancer selects one lease holder, roles and cumulative counters survive takeover, and the previous primary stays fenced. |

### Individual restart drill

Use `recovery.engine_restart_policy = individual` and the prerequisites in [Restart one engine](03-Restart-Engines.md#7-restart-one-engine).

1. Drain the engine.
2. Replace it through the production supervisor.
3. Save the pre-stop drain observation, both process identities, and the supervisor output.
4. Request readmission with the previous profile loaded, through `curl` with `--fail` removed.
5. Keep the rejection: HTTP 409, the profile generation error, and `accepts_new = false`.
6. [Activate the fresh profiles](03-Restart-Engines.md#activate-replacement-profiles).
7. Request explicit readmission.
8. Save the successful response.
9. Confirm it shows:
    - `state = active` and `accepts_new = true`;
    - a newer process start;
    - every [readmission check](03-Restart-Engines.md) passing.
10. Send a routed request.
11. Confirm its placement includes the returned engine.
12. Match the client response and request ID to the [journal's terminal event](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) and the router's cumulative counters.
13. Keep state snapshots from before and after the routed request.

### Whole-wave drill

Use `recovery.engine_restart_policy = whole_wave` and the setup in [Engine restart and process replacement](03-Restart-Engines.md), including `ROUTER_URL` and `RUN_DIR`.

1. [Drain the wave](03-Restart-Engines.md#81-drain-the-wave).
2. Before stopping anything, save the HTTP 503 readiness reading and the drain identities.
3. Confirm zero resident work and `wave.ready_to_stop = true`.
4. Restart every engine and attestation sidecar from the recorded build through its supervisor.
5. While the wave remains held, [activate replacement profiles](03-Restart-Engines.md#activate-replacement-profiles).
6. Verify that resume preserves every drain identity and the whole-wave hold.
7. Stop one member's attestation sidecar with the recorded supervisor command.
8. Confirm its engine stays healthy and the other sidecars keep running.
9. Request readmission and save its failure response:

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

10. Confirm the reported errors:
    - the stopped sidecar's member: an attestation failure;
    - every other member: another wave engine failed validation.
11. In a second terminal, set the same `ROUTER_URL` and `RUN_DIR` values.
12. Start this sampler while the whole-wave hold is active:

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

    The sampler:

    - exits after complete readmission;
    - fails on partial readmission;
    - ends early on Ctrl+C.

13. Restart the failed member's sidecar with the attestation inputs from profiling.
14. Leave the replacement engine running.
15. Check that its attestation endpoint returns the digest from the new profile.
16. Check that the whole-wave hold remains active.
17. [Request and verify whole-wave readmission](03-Restart-Engines.md#82-restart-the-fleet).
18. Verify that every member passed every [readmission check](03-Restart-Engines.md).
19. Find the journal's `engine_lifecycle` event with `action = wave_readmitted`.
20. Send a routed request.
21. Match its client result to the journal and the router's cumulative counters.
22. Save the samples, readmission response, and lifecycle events.

### Unplanned whole-wave hold drill

`whole_wave` needs a [liveness sweep](03-Restart-Engines.md#9-detect-process-replacement): `recovery.liveness_every` above zero, such as the default `10`.

Set `recovery.engine_restart_policy = whole_wave`.

1. Wait until every engine is admitted and idle.
2. Stop one attestation sidecar through the production supervisor.
3. Wait until the router withdraws readiness and excludes every member.
4. Save the `process_excluded` and `restart_required` lifecycle events.
5. Follow the steps in [Recover an unplanned whole-wave hold](03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold).
6. Check that the drain recorded identities before member replacement.
7. Verify full readmission and a routed request.

To test the already-stopped-engine case:

1. Stop an engine before requesting the drain.
2. Keep the failed drain response with its missing identity, the process start time from identity collection, and the successful retry.
3. Replace that process after the drain.
4. Record the trigger and the branch tested.

### Restore service after the drill

1. Confirm the final profile store covers the serving fleet's current process generations.
2. Keep the measurements activated for readmission and the evidence for unchanged engines.
3. If the drill used an isolated subset:
    1. Complete the subset's held readmission.
    2. Assemble the complete serving fleet's profile store.
    3. Run [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) with the original fleet configuration and that profile store.
    4. Stop the temporary routers before starting the serving router.
4. Confirm the intended router controls the complete fleet.
5. Confirm `/ready` returns HTTP 200.
6. Send a routed request.
7. Match its result to the journal and the router's counters.
8. Restore external admission.

| Later change | Procedure | Evidence to keep |
| --- | --- | --- |
| Another process replacement | Fresh measurements and another activation | |
| Router replacement with an outstanding hold on the same fleet | [Profile activation with resume](03-Restart-Engines.md#activate-replacement-profiles) | Both journals and the state snapshots |
