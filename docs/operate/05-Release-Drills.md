# Release drills

## 11. Validate every release

Run the release drills on an idle fleet with external admission closed:

1. Run the restart drill for your `recovery.engine_restart_policy`.
2. Test [router failover](../troubleshoot/02-Router-Recovery.md#router-failover) through the production supervisor and load balancer.
3. [Restore service](#restore-service-after-the-drill).

Keep the release, fleet configuration, profiles, engine build, supervisor commands, and drill results in the private deployment directory.

Earlier drill results count when they meet the pass conditions.

Test the production supervisor when an earlier run used another launcher.

### Release drill pass conditions

The individual engine restart drill passes when:

- The drain completes.
- Stale profiles block readmission.
- Fresh profiles load with the hold preserved.
- A routed request reaches the returned engine.

The whole-wave restart drill passes when:

- The router withdraws readiness before the first stop.
- One failed member holds the whole wave.
- Every member returns together after validation.

The unplanned whole-wave hold drill passes when the drain records current identities for the whole excluded wave before member replacement and readmission.

The router failover drill passes when:

- The load balancer selects one lease holder.
- Roles and cumulative counters survive takeover.
- The previous primary stays fenced.

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
9. Confirm it shows `state = active`, `accepts_new = true`, a newer process start, and every [readmission check](03-Restart-Engines.md) passing.
10. Send a routed request.
11. Confirm its placement includes the returned engine.
12. Match the client response and request ID to the [journal's terminal event](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) and the router's cumulative counters.
13. Keep state snapshots from before and after the routed request.

### Whole-wave drill

Use `recovery.engine_restart_policy = whole_wave` and the setup in [Engine restart and process replacement](03-Restart-Engines.md), including `ROUTER_URL` and `RUN_DIR`.

#### Drain and restart

1. [Drain the wave](03-Restart-Engines.md#81-drain-the-wave).
2. Save the HTTP 503 readiness reading.
3. Save the drain identities.
4. Confirm zero resident work and `wave.ready_to_stop = true`.
5. Restart every engine from the recorded build through its supervisor.
6. Restart every attestation sidecar from the recorded build through its supervisor.
7. While the wave remains held, [activate replacement profiles](03-Restart-Engines.md#activate-replacement-profiles).
8. Verify that resume preserves every drain identity and the whole-wave hold.
#### Fail one member

9. Stop one member's attestation sidecar with the recorded supervisor command.
10. Confirm its engine stays healthy.
11. Confirm the other sidecars keep running.
12. Request readmission, saving the failure response:

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

13. Confirm the stopped sidecar's member reports an attestation failure and every other member reports that another wave engine failed validation.
#### Sample the hold

14. In a second terminal, set the same `ROUTER_URL` and `RUN_DIR` values.
15. Start this sampler while the whole-wave hold is active:

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

    The sampler exits after complete readmission, fails on partial readmission, and ends early on Ctrl+C.

#### Recover and verify

16. Restart the failed member's sidecar with the attestation inputs from profiling.
17. Leave the replacement engine running.
18. Check that its attestation endpoint returns the digest from the new profile.
19. Check that the whole-wave hold remains active.
20. [Request and verify whole-wave readmission](03-Restart-Engines.md#82-restart-the-fleet).
21. Verify that every member passed every [readmission check](03-Restart-Engines.md).
22. Find the journal's `engine_lifecycle` event with `action = wave_readmitted`.
23. Send a routed request.
24. Match its client result to the journal and the router's cumulative counters.
25. Save the samples, readmission response, and lifecycle events.

### Unplanned whole-wave hold drill

`whole_wave` needs a [liveness sweep](03-Restart-Engines.md#9-detect-process-replacement): `recovery.liveness_every` above zero, default `10`.

1. Set `recovery.engine_restart_policy = whole_wave`.
2. Wait until every engine is admitted and idle.
3. Stop one attestation sidecar through the production supervisor.
4. Wait for the router to withdraw readiness.
5. Wait for the router to exclude every member.
6. Save the `process_excluded` and `restart_required` lifecycle events.
7. [Recover the unplanned whole-wave hold](03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold).
8. Check that the drain recorded identities before member replacement.
9. Verify full readmission and a routed request.

To test the already-stopped-engine case:

1. Stop an engine before requesting the drain.
2. Keep the failed drain response with its missing identity, the process start time from identity collection, and the successful retry.
3. Replace that process after the drain.
4. Record the trigger and the branch tested.

### Restore service after the drill

1. Confirm the final profile store covers the serving fleet's current process generations.
2. Keep the measurements activated for readmission.
3. Keep the evidence for unchanged engines.
4. If the drill used an isolated subset:
    1. Complete the subset's held readmission.
    2. Assemble the complete serving fleet's profile store.
    3. Run [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) with the original fleet configuration and that profile store.
    4. Stop the temporary routers before starting the serving router.
5. Confirm the intended router controls the complete fleet.
6. Confirm `/ready` returns HTTP 200.
7. Send a routed request.
8. Match its result to the journal and the router's counters.
9. Restore external admission.

| Later change | Procedure | Evidence to keep |
| --- | --- | --- |
| Another process replacement | Fresh measurements, another activation | |
| Router replacement with an outstanding hold on the same fleet | [Profile activation with resume](03-Restart-Engines.md#activate-replacement-profiles) | Both journals and the state snapshots |
