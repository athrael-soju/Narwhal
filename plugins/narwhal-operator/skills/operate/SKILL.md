---
name: narwhal-operate
description: Guide Narwhal engine restart, whole-wave recovery, router failover, upgrade, rollback, and release drills. Use when an operator must coordinate a documented lifecycle procedure with the site supervisor.
---

# Operate a Narwhal fleet

Read the [Operator contract](../../../../docs/Operator-Plugin.md) and [production procedures](../../../../docs/Operate.md) from the approved Narwhal revision. Choose the specific procedure from the request and current state. Site automation owns process replacement, router start and stop, ingress, and load balancer changes; Narwhal owns lifecycle HTTP actions, placement, and admission.

## Establish the procedure

1. Obtain the named procedure, current deployment set, exact targets, configured `recovery.engine_restart_policy`, trusted control endpoints, site supervisor, maintenance scope, time and disruption budget, and action-specific approval. In `plan` mode, return the ordered checks, actions, approvals, and expected observations. In `observe` mode, capture state without dispatching changes.
2. Check installed build identity and the contract manifest, including `narwhal.command-result` and handoff versions. Compare release, fleet configuration, profiles, evidence, and supported persisted state across the router pair. Verify one active lease owner using `/ready` and inspect `/health`, `/narwhal/state`, and `/narwhal/lifecycle` before a change. Record `blocked` or `unsupported` for missing or incompatible prerequisites.
3. Open only the applicable [router pair](../../../../docs/operate/01-Start-Routers.md), [monitoring](../../../../docs/operate/02-Monitor.md), [engine lifecycle](../../../../docs/operate/03-Restart-Engines.md), or [upgrade and drill](../../../../docs/operate/04-Upgrade-and-Validate.md) page. Use its exact policy branch and pass conditions. Keep one mutation workflow within a maintenance scope at a time, as required by the host adapter.

`execute` requires the host adapter's grants and an approval bound to target, parameters, deployment-set digest, scope, validity window, and disruption budget. Before each active probe, lifecycle POST, or site handoff, check that the approval covers that action. Save a pre-action snapshot and its HTTP status. Give a site-supervised action to the configured supervisor and retain its receipt; do not invent a process-manager command.

## Engine lifecycle

For `individual` policy, [drain the named engine](../../../../docs/operate/03-Restart-Engines.md#7-restart-one-engine) through the active router. Poll `/narwhal/lifecycle` until that engine reports `ready_to_stop = true`. The supervisor then replaces the engine and attestation sidecar. Verify the new process identity, engine HTTP endpoints, and process-bound attestation before POST readmission. Record every readmission gate. HTTP 409 keeps the engine blocked; report the named gate and use its evidence to guide repair.

For `whole_wave` policy, [drain the wave](../../../../docs/operate/03-Restart-Engines.md#8-restart-an-engine-wave), then wait for both withdrawn `/ready` and `wave.ready_to_stop = true`. The supervisor replaces every engine and sidecar from one immutable build. Request wave readmission after process identities and attestations have been checked. The entire wave stays excluded until every gate and the role-permitted KV ring pass. For an unplanned hold, follow the runbook's identity-capture recovery sequence before any replacement.

After a drain timeout, process loss, transport loss, or missing supervisor receipt, record `interrupted`. Inspect current lifecycle state and supervisor receipts to establish effects before another action. A failed postcondition records `failed` with the gate or predicate. Start repair or rollback under its own approval and attempt ID.

## Router lifecycle

For [unplanned router failover](../../../../docs/troubleshoot/02-Router-Recovery.md#router-failover), query both routers, identify the single lease holder, and compare its lease epoch, roles, and cumulative counters with the last persisted handoff. Direct the load balancer to that router only after `/ready` returns HTTP 200. A recovered former primary starts as standby and returns HTTP 503 from `/ready`. Keep admission stopped if handoff is stale or incompatible, or both routers refuse readiness.

For [upgrade or rollback](../../../../docs/operate/04-Upgrade-and-Validate.md#10-upgrade-and-rollback), check both routers' handoff contracts and current lease owner. A rolling transition uses compatible handoff versions; a version change follows the runbook's maintenance sequence. After a site-supervised stop or start, confirm the expected `/health` and `/ready` status, lease epoch, and single ready backend. For rollback, restore code, fleet configuration, profiles, and a state version supported by the restored build as one deployment set.

The [release drills](../../../../docs/operate/04-Upgrade-and-Validate.md#11-validate-every-release) require an idle fleet, the production supervisor, and load balancer evidence. Verify the configured engine-restart path and router failover pass conditions before production admission. [Router failover qualification](https://github.com/athrael-soju/Narwhal/issues/49) and [engine restart qualification](https://github.com/athrael-soju/Narwhal/issues/50) track live evidence for those paths.

Write before and after state, HTTP responses, process identities, lease observations, supervisor receipts, and native artifacts under `procedures/<procedure>/<attempt-id>/`. Record a `narwhal.operator-evidence` version 1 attempt and update `decision.json`. Report the selected procedure, current decision, private evidence path, exact pending or failed condition, next owner, and verification needed for recovery. Synthetic tests establish code behavior within their scope; release qualification uses observations from the running fleet.
