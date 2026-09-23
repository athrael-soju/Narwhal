# Engine and whole-wave recovery

## Engine failure

### One engine failed unexpectedly

Confirm that the engine is ejected or quarantined and that surviving engines continue receiving work. Save the engine boot log and supervisor exit reason before restarting the process.

Read:

```text
recovery.engine_restart_policy
```

For `individual` recovery:

1. Start the engine far enough to verify its HTTP endpoints.
2. Restart its sidecar through the configured process manager so the sidecar binds the new process identity.
3. Inspect the sidecar log.
4. Repair any named endpoint or contract failure.
5. Follow `/narwhal/lifecycle` while the router runs the health, attestation, model, generation, role-permitted KV, and final-health gates.
6. Confirm `accepts_new: true`.
7. Confirm that the engine ejection has cleared.

If validation enters `blocked`, repair the engine and call:

```text
/narwhal/lifecycle/readmit
```

The router clears the ejection after every recovery gate passes.

For `whole_wave`, use the complete-wave procedure below for drain and readmission.

### Planned restart of one engine

Use the [drain sequence](../operate/03-Restart-Engines.md#7-restart-one-engine).

The external supervisor may stop the engine after the lifecycle state reports:

```text
ready_to_stop: true
```

At that point new placement has stopped and resident work has drained.

Readmission requires a newer process identity and reruns the recovery gates before the engine resumes placement.

## Whole-wave recovery

Use whole-wave recovery for:

- `recovery.engine_restart_policy: whole_wave`;
- stale-peer assertions;
- transfer stalls that kill a peer;
- engine-generation mismatches.

Start a lifecycle whole-wave drain so the router stops new traffic.

If drain identity capture fails for an engine, use the [unplanned whole-wave procedure](../operate/03-Restart-Engines.md#8-restart-an-engine-wave) to restore that endpoint, then retry the drain.

Wait for both conditions:

```text
wave.ready_to_stop: true
```

and:

```text
/ready -> HTTP 503
```

Stop every engine process tree through the external supervisor.

Before restarting the wave, verify that accelerator memory allocations belong to the intended worker processes.

Launch every engine from the same immutable image and launch contract. Start a fresh attestation sidecar for every engine process, then submit whole-wave readmission.

Restore ingress after the KV ring and final-health gates pass and `/ready` returns HTTP 200.

Run the [post-recovery drills](../Troubleshoot.md#after-recovery) after the repaired fleet returns to service.
