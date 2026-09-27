# Engine and whole-wave recovery

These lifecycle procedures require a complete
[`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract).

## Engine failure

### One engine failed unexpectedly

Confirm that the engine is ejected or quarantined and that surviving engines continue receiving work. Save the engine boot log and supervisor exit reason before restarting the process.

Check `recovery.engine_restart_policy` and follow the matching procedure below.

For `individual` recovery:

1. Start the engine far enough to verify its HTTP endpoints.
2. Restart its sidecar through the configured process manager so the sidecar binds the new process identity.
3. Inspect the sidecar log.
4. Repair any named endpoint or contract failure.
5. If the engine process changed, [activate fresh profiles while preserving its hold](../operate/03-Restart-Engines.md#activate-replacement-profiles), then request readmission.
6. Follow `/narwhal/lifecycle` while the router runs the health, attestation, profile-generation, model, generation, role-permitted KV, and final-health gates.
7. Confirm `accepts_new: true` and that the engine ejection has cleared.

If validation enters `blocked`, repair the failure named in
`engines.<id>.error`, then send a `POST` request to:

```text
/narwhal/lifecycle/readmit
```

The router clears the ejection after every recovery gate passes.

For `whole_wave`, use the complete-wave procedure below for drain and readmission.

### Planned restart of one engine

Follow the [individual restart sequence](../operate/03-Restart-Engines.md#7-restart-one-engine).
Wait until `/narwhal/lifecycle` reports `ready_to_stop: true`, then stop the
engine through its supervisor. Start the replacement with a newer process
identity and activate its fresh profiles while preserving the hold. Request
readmission; Narwhal reruns the recovery checks before placing new work on
that engine.

## Whole-wave recovery

Use whole-wave recovery for:

- `recovery.engine_restart_policy: whole_wave`;
- stale-peer assertions;
- transfer stalls that kill a peer;
- process replacements that invalidate shared peer state.

Under the `individual` policy, a stale profile holds the affected engine.
Activate its fresh profiles and request individual readmission.

Start a lifecycle whole-wave drain so the router stops new traffic.

If drain identity capture fails for an engine, use the [unplanned whole-wave procedure](../operate/03-Restart-Engines.md#8-restart-an-engine-wave) to restore that endpoint, then retry the drain.

Wait for both conditions:

- `wave.ready_to_stop` is `true`.
- `/ready` returns HTTP 503.

Stop every engine process tree through the external supervisor.

Before restarting the wave, verify that accelerator memory allocations belong to the intended worker processes.

Launch every engine from the same immutable image and launch contract. Start
a fresh attestation sidecar for every engine process. While the wave remains
held, [activate the replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles),
then request whole-wave readmission.

Restore ingress after the KV ring and final-health gates pass and `/ready` returns HTTP 200.

Run the [post-recovery drills](../Troubleshoot.md#after-recovery) after the repaired fleet returns to service.
