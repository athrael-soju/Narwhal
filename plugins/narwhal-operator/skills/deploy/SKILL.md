---
name: narwhal-deploy
description: Guide a new or resumed Narwhal fleet deployment through its documented gates. Use for deployment qualification, gate recovery, and deciding which gate to repeat after an input changes.
---

# Deploy a Narwhal fleet

Use the approved Narwhal checkout and its [deployment runbook](../../../../docs/Deploy.md). Read the [Operator contract](../../../../docs/Operator-Plugin.md) before recording decisions. Open the current gate page when that gate is selected; do not load every gate page by default. The installed revision of the runbook controls commands and acceptance conditions.

## Select the work

1. Obtain the deployment ID, approved source revision, deployment set, private evidence root, permitted hosts and endpoints, execution mode, workload and time budget, caller authorization, and site supervisor. Record `blocked` and name any missing input required for the next action.
2. Inspect prior `decision.json` and gate attempts in `runs/operator/<deployment-id>/<run-id>/`. Match their deployment-set digest and process generations to the current set. Preserve the starting state of a new or resumed run. Apply the [change-to-repeat matrix](../../../../docs/Deploy.md#deployment-sequence) when a measured input changed; state which gate must be repeated and which evidence remains current.
3. Select the first required gate without a current `passed` record. Gates run A, B, C, D, E, F, G. A failed, degraded, blocked, interrupted, or unsupported predecessor stops dependent gates. Report the exact recovery point, including the gate and last attempt ID.
4. Before dispatch, check installed build identity and supported `narwhal.command-result` versions as specified in the Operator contract. Record `blocked` for unverified identity and `unsupported` for a verified incompatible build or schema. Repeat these checks after installation or deployment-set changes.

`plan` returns the selected gate, required evidence, proposed commands, approvals, and site handoffs. `observe` reads approved state and records evidence. `execute` dispatches approved probes and Narwhal actions within the supplied budget. The host adapter enforces the grants for `active-probe`, `narwhal-mutate`, and `site-supervised`; a skill instruction alone grants none of them. Site automation performs installation, process changes, ingress, and monitoring changes and returns a receipt.

## Run one gate

Open only the selected page in [Gates A–G](../../../../docs/Deploy.md#deployment-sequence). Use its host context, prerequisites, commands, acceptance conditions, and failure procedure. Run the minimum commands that establish the current gate's missing predicates; reuse matching evidence from the current deployment set. Do not substitute management-workstation hardware for engine-host evidence.

| Gate | Page | Work to establish |
| --- | --- | --- |
| A | [Discover](../../../../docs/deploy/01-Discover.md) | Approved inputs, actual host and checkpoint inventory, access, and launch policy. |
| B | [Install](../../../../docs/deploy/02-Install.md) | Prepared bundle identity and site-supervised installation receipts. |
| C | [Engines](../../../../docs/deploy/03-Validate-Engines.md) | Checked plans, running engine identities, HTTP probes, and live cache captures for every engine. |
| D | [Fabric](../../../../docs/deploy/04-Qualify-Fabric.md) | Source budgets and measured directed edges against the current route and cache layout. |
| E | [Attest](../../../../docs/deploy/05-Attest.md) | Process-bound attestations, sidecar responses, and the final fleet engine contract. |
| F | [Profile and preflight](../../../../docs/deploy/06-Profile-and-Preflight.md) | Current profiles and samples, full-mesh preflight, and a passing live KV contract. |
| G | [Serve and measure](../../../../docs/deploy/07-Serve-and-Measure.md) | Router and monitoring observations, private-path trial, reconciliation, and post-load ring. |

Create a fresh attempt directory under `gates/<letter>/<attempt-id>/` before invoking a command or asking the supervisor to act. Use owner-only file permissions. Record starting state, target host or stable alias, full source revision, command and arguments, start and finish times, process exit, stdout and stderr, native result, output artifacts and SHA-256 digests, and any supervisor receipt. Keep private addresses, credentials, raw logs, request content, and unrestricted configuration in the private environment.

Run finite Narwhal commands with `--format json`. Validate the result envelope, schema version, status and exit-code pairing before interpreting `data`. A `success` result establishes only that command's operation; evaluate every runbook predicate and required artifact before marking the gate `passed`. For deployment helpers and fabric tools, use their documented exit codes and inspect their output artifacts. GET and completion probes need their HTTP status and response evidence. Do not treat a command's diagnostic prose as a stable decision code.

Write `record.json` for the attempt and update the root `decision.json` with the selected next step. Use `narwhal.operator-evidence` version 1 and the fields in the Operator contract. Keep each failed attempt and create a new attempt ID for a retry. If the process stops before the record is complete, retain partial files and record `interrupted` on recovery.

| Decision | Next action |
| --- | --- |
| `passed` | Advance to the next gate when all current predicates and evidence hold. |
| `failed` | Name the failed predicate and source artifact, investigate its cause, then start a new attempt at the affected gate. |
| `degraded` | Identify covered and uncovered checks; wait for a permitted scoped exception or a complete retry. |
| `blocked` | Name the missing input, approval, predecessor evidence, site capability, or evidence storage condition. |
| `interrupted` | Reconcile current state and supervisor receipts before deciding whether another attempt is safe. |
| `unsupported` | Retain the raw result privately and identify the build or schema mismatch. |

Report the selected gate, decision, evidence path, failed or pending predicate, next owner, and exact recovery point. A synthetic test establishes skill behavior within that test; fleet qualification requires the runbook's live host, fabric, engine, and workload evidence.
