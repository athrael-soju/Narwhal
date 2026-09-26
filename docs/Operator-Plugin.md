# Narwhal Operator contract

This page defines the Operator v1 design for deployment gates, production procedures, evidence, and action ownership. The Operator will check a deployment set, invoke approved Narwhal commands and HTTP endpoints, record native results, and report the next step.

For deployment, the Operator evaluates Gates A–G in order, from discovery through serving validation. For production, it observes router state, coordinates engine lifecycle and release drills with the site supervisor, and collects incident bundles. Each attempt produces a private evidence record and a decision that tells the operator what can proceed.

[Deploy a fleet](Deploy.md) defines gates A–G. [Operate Narwhal](Operate.md) defines production procedures. [Command results](Command-Results.md) and [diagnostic bundles](Diagnostic-Bundles.md) define their machine-readable evidence. Narwhal runs checks, profiling, attestation, placement, leases, lifecycle transitions, and diagnostic collection. The site supervisor manages host access, processes, ingress, and monitoring policy.

## Package and host boundary

The Operator package will use `plugins/narwhal-operator/`:

```text
plugins/narwhal-operator/
  plugin.json
  README.md
  skills/
    inspect/SKILL.md
    deploy/SKILL.md
    operate/SKILL.md
    diagnose/SKILL.md
  references/
    Operator-Plugin.md
```

`plugin.json` will contain these fields. Package paths use forward slashes relative to the package root.

| Required field | Type and value | Purpose |
| --- | --- | --- |
| `format` | String, exactly `narwhal.operator-plugin` | Identify the portable descriptor. |
| `format_version` | Integer, exactly `1` | Select these validation rules. |
| `name` | String, exactly `narwhal-operator` | Identify the package. |
| `version` | Nonempty release identifier string | Identify the packaged skill revision. |
| `contract` | Relative path, exactly `references/Operator-Plugin.md` | Locate this contract in the package. |
| `skills` | Object with exactly `inspect`, `deploy`, `operate`, and `diagnose` | Map each workflow to its `skills/<name>/SKILL.md` file. |
| `requires.cli_versions` | Nonempty array of distinct, exact Narwhal distribution version strings | Select qualified command implementations. |
| `requires.command_result` | Object with `schema: narwhal.command-result` and integer `schema_version: 1` | Select the command-result parser. |
| `capabilities` | Object with the same four keys as `skills` | Declare each workflow's permitted action classes below. |

The `requires` object contains exactly `cli_versions` and `command_result`; `command_result` contains exactly `schema` and `schema_version`. The descriptor parser must reject duplicate JSON keys, additional fields at every level, wrong types, unsupported format versions, and empty or duplicate array values. It must resolve each `contract` and `skills` path within the package root, reject absolute paths and parent traversal, and require a regular file at each resolved path. Symlinks must resolve inside the package root. The package build copies this canonical document from `docs/Operator-Plugin.md` to `references/Operator-Plugin.md` and verifies equal SHA-256 digests.

The `capabilities` arrays declare upper bounds. The host adapter reads this project descriptor, translates it into host-specific installation metadata, and grants the intersection of those classes with site policy. Active probes, Narwhal mutations, and site-supervised handoffs also require action-specific invocation approval. The adapter installs the skills, resolves credentials, and enforces those grants. At invocation, the skills receive fleet addresses and deployment inputs. A manual adapter produces a plan and handoff for a human operator.

Before execution, compare each installed command's `--version` output with the package's `requires.cli_versions` and the approved deployment set. Check `narwhal-check`, `narwhal-profile`, `narwhal-engine`, and `narwhal` in the environment that will run each one. The host adapter must verify that each executable resolves to the approved installation. Compare its installed artifact digest with the approved receipt, or compare its full source revision and clean checkout with the approved bundle hash and revision. Gate B's prepared manifest supplies the bundle hash and revision for runbook installations; other installation methods need an equivalent site receipt. Record `blocked` when installed build identity cannot be established, and `unsupported` when a verified build differs from the approved one.

Validate the `narwhal.contract-manifest` envelope returned by `narwhal-check --print-contract-versions`. It must advertise `narwhal.command-result` with readable version 1 and the persisted contracts required by the workflow. Repeat build and contract checks after installation or deployment-set changes.

At package release, populate `requires.cli_versions` with exact versions that pass conformance checks against their installed artifacts. For each listed version, retain fixtures for the command surface used by the skills, the contract manifest, and the `narwhal.command-result` status and exit-code mapping. Store the fixture digests with the package release. The package metadata carries the qualified version list; this document defines the selection and validation rules.

Run finite `narwhal-check`, `narwhal-profile`, `narwhal-engine`, and `narwhal dev` operations with `--format json`. The [command-result contract](Command-Results.md) defines one stdout object with `schema`, `schema_version`, `command`, `operation`, `status`, `exit_code`, `data`, `artifacts`, and `errors`; progress goes to stderr. Preserve both streams and the process exit, validate the envelope and status/exit-code pairing, then interpret `data`. Inspect running `narwhal-serve` and `narwhal-attest` processes through their HTTP and persisted-state interfaces. Interpret deployment helper and fabric tool outputs according to their own documented exit codes. On an unrecognised schema version, incomplete result, or incompatible build, record `unsupported` and stop the dependent workflow.

For diagnostic collection, validate the `narwhal.diagnostic-bundle` version 1 manifest, then inspect its `success` or `partial` status and per-source outcomes. For another manifest version, record `unsupported` and retain the collector result for inspection.

## Inputs and action ownership

Each invocation supplies a workflow ID, deployment ID, approved source revision and deployment-set references, private evidence root, permitted hosts and endpoints, mode (`plan`, `observe`, or `execute`), time and workload budget, and caller authorization context. A deployment set binds the release, fleet configuration, profile store, model, engine image and process generations, topology, and applicable evidence. Procedure-specific inputs appear below. When a required input is absent, record `blocked` and name the input needed to proceed.

| Action class | Examples | Owner and authorization |
| --- | --- | --- |
| `observe` | Approved files; bounded GET `/health`, `/ready`, `/narwhal/state`, `/narwhal/lifecycle`, `/metrics` | Operator within its read scope. |
| `evidence-write` | Run records and snapshots under the approved evidence root | Operator within its private write scope. |
| `active-probe` | `narwhal-check`, live profiling, fabric tests, completions, load trials | Narwhal or the documented test tool under a workload and time budget. |
| `narwhal-mutate` | POST lifecycle drain/readmit; `attestation_contract finalize-fleet`; `narwhal-profile --overwrite` | Narwhal's documented interface under action-specific approval. |
| `site-supervised` | Install releases; start/stop processes; change tunnels, ingress, monitoring or load balancers | Site supervisor. The Operator records its receipt and postconditions. |

`plugin.json` assigns these action-class upper bounds:

| Skill | `capabilities` array |
| --- | --- |
| `inspect` | `observe`, `evidence-write` |
| `deploy` | `observe`, `evidence-write`, `active-probe`, `narwhal-mutate`, `site-supervised` |
| `operate` | `observe`, `evidence-write`, `active-probe`, `narwhal-mutate`, `site-supervised` |
| `diagnose` | `observe`, `evidence-write` |

The descriptor parser must reject an unknown action class or a class outside the listed upper bound for its skill. An action that spans classes requires each applicable grant; profiling, for example, uses `active-probe` and `evidence-write`.

`plan` produces an action sequence and required approvals. `observe` reads approved state and writes private evidence. `execute` runs approved active probes and Narwhal actions and requests site-supervised steps. Approval binds target, parameters, deployment-set digest, scope, validity window, and disruption budget. The host adapter enforces approval and serializes mutation workflows within one maintenance scope. Narwhal's lease controls router admission.

| Skill workflow | Additional inputs                                                                            | Output and evidence path                                                                          | Response                                                                                                                         |
| -------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `inspect`      | Installed command environments, deployment-set references and permitted control endpoints    | Build identity, contract registry and inventory in `compatibility.json` and `procedures/inspect/` | On incompatible identity or schema, record `unsupported` or `blocked` and identify the correction.                               |
| `deploy`       | Gate A–G inputs, predecessor records, workload budgets and site supervisor                   | Per-gate attempts in `gates/<letter>/` and final decision                                         | A failed, degraded, interrupted, blocked or unsupported required gate stops progression.                                         |
| `operate`      | Current deployment set, named procedure, exact target, policy and site approval              | Before/after state and supervisor receipts in `procedures/<procedure>/`                           | Missing receipt, uncertain effects, failed postcondition or contract mismatch stops further actions.                             |
| `diagnose`     | Incident scope, router URL, source selection, fresh private output path and inclusion policy | Native bundle and collector result in `diagnostics/<attempt-id>/`                                 | Record partial collection as `degraded`, unfamiliar bundle schema as `unsupported`, and the audience review required for export. |

## Evidence and decisions

Use `runs/operator/<deployment-id>/<run-id>/` in a checkout or an equivalently private site directory. `runs/` is ignored by Git and is the project's working-output location. Create owner-only directories and files. A separate attempt directory preserves each failure and retry:

```text
<run-id>/
  deployment-set.json
  compatibility.json
  gates/A/<attempt-id>/...       # B through G use the same shape
  procedures/<procedure>/<attempt-id>/...
  diagnostics/<attempt-id>/...
  decision.json
```

Each attempt writes `record.json`; the root `decision.json` references those records and reports the overall decision. The Operator evidence record uses `schema: narwhal.operator-evidence` and integer `schema_version: 1`.

| Required field | Type and content |
| --- | --- |
| `run_id`, `workflow`, `attempt_id` | Strings identifying the run, workflow, and attempt. |
| `deployment_set_sha256` | SHA-256 digest of the deployment-set record. |
| `started_at`, `finished_at` | UTC ISO 8601 timestamps. |
| `decision`, `reason` | Decision from the table below and its supporting predicate or prerequisite. |
| `action_classes` | List of action classes from [action ownership](#inputs-and-action-ownership). |
| `approval_ref` | Host approval reference; `null` for observation. |
| `native_results` | References to command or HTTP receipts, with actual exit/status and schema identity/version when present. |
| `artifacts` | Roles, paths or approved external references, SHA-256 digests, and sensitivity labels. |
| `next_owner` | Owner responsible for the next action. |

Retain raw stdout, stderr, HTTP responses, native files, and supervisor receipts alongside the interpretation. Reference operational artifacts in place when copying would expose secrets or change their lifecycle. Give each retry a new attempt ID and record its predecessor. Match gate evidence to the current deployment set and process generations. Evaluate every observation required by the gate procedure before recording `passed`. Native `narwhal.command-result` documents remain separate artifacts.

| Decision      | Agent behavior                                                                                                                                                                                                           |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `passed`      | All required predicates and evidence for the current deployment set hold; the next approved step may proceed.                                                                                                            |
| `failed`      | A documented gate or postcondition failed. Retain evidence, stop dependent steps, and diagnose the named cause before a new attempt.                                                                                     |
| `degraded`    | Record the covered and uncovered checks from a native result or partial observation. Pause production advancement pending a source-permitted, scoped exception or a complete retry.                                      |
| `blocked`     | Name the required input, approval, predecessor evidence, site capability, or evidence storage condition and wait for it before dispatch.                                                                                 |
| `interrupted` | Completion or effects are uncertain after timeout, cancellation, transport loss, or process loss. Preserve partial evidence, reconcile current state and supervisor receipts, then decide whether a new attempt is safe. |
| `unsupported` | Retain raw data privately, identify the installed build or contract mismatch, and wait for a compatible version before interpretation.                                                                                   |

After interruption of a potentially mutating request, inspect the documented current-state endpoint and site receipt to establish its effects. Then decide whether a new attempt is needed. Remediation and rollback use their own approval and evidence. Changed hardware, model, image, runtime, profile, route, transport, or process generation triggers the affected gate work specified in [Deploy](Deploy.md#deployment-sequence).

## Deployment gates A–G

Run gates in [deployment order](Deploy.md#deployment-sequence). Each row names the inputs, Narwhal surface, evidence, and next-step condition. Evidence prefixes are relative to this run's root and contain one directory per attempt. Apply the version and decision rules above to each gate; advance after its predecessor passes.

| Gate | Input and operation | Pass evidence | Hold condition |
| --- | --- | --- | --- |
| [A: Discover](deploy/01-Discover.md) | Approved `.env`, image, checkpoint and host access; run discovery, `plan`, and `check-access`. | Matching checkpoint tree, generated configuration and access logs in `gates/A/`. | Input, hardware, key or access mismatch. |
| [B: Install](deploy/02-Install.md) | A's configuration and approved commit; run `deploy_hosts.py prepare` and site-supervised `install --run`. | Prepared manifest, bundle hashes and installed revision in `gates/B/`. | Transfer or revision mismatch. |
| [C: Engines](deploy/03-Validate-Engines.md) | Installed roles and pinned launch plans; check, start, probe and capture each engine. | Checked plan, container ID, HTTP captures and live `cache-layout.json` in `gates/C/`. | Device, runtime, listener or process mismatch. |
| [D: Fabric](deploy/04-Qualify-Fabric.md) | C's cache layouts and directed routes; calculate source budgets and measure each directed edge. | Budget, route fingerprint, sample and edge verdict in `gates/D/`. | Invalid sample or rate below its source budget. |
| [E: Attestation](deploy/05-Attest.md) | C's process captures and D's geometry; generate, serve and finalise the engine contract. | Process-bound attestation, sidecar responses and final fleet `engine_contract` in `gates/E/`. | Sidecar or contract mismatch. |
| [F: Profile and preflight](deploy/06-Profile-and-Preflight.md) | E's running engines and workload sweep; run `narwhal-profile` and full-mesh `narwhal-check`. | Profiles, samples, command results and passing preflight in `gates/F/`. | Skipped or failed gate, stale process, transfer error or infeasible SLO. |
| [G: Serve and measure](deploy/07-Serve-and-Measure.md) | F's passing preflight; start router and monitoring, run the private-path trial and post-load ring. | Endpoint captures, reconciled requests, monitoring series and passing ring in `gates/G/`. | Missing scrape, unreconciled request, failed ring or unmet site threshold. |

Before Gate G, the site selects acceptance thresholds and a workload. The runbook's 2 s TTFT, 33.3 ms TPOT, and 95% attainment figures are candidates for that selection. Gate D uses live directed-link measurements to establish fleet bandwidth against the chosen workload budget.

## Production procedures

The [production startup checklist](Operate.md#production-startup-checklist) uses the same deployment set on both routers. Use the detailed procedures below for ordering and pass conditions.

| Checklist steps | Operator mapping and site action                                                                                                                          |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1–2             | `inspect` binds the release, fleet, profiles and evidence; compare both routers' handoff contracts with `narwhal-check --print-contract-versions`.        |
| 3–4             | Site establishes private control interfaces, trusted ingress rewriting and the shared lease domain; `inspect` records the approved references and checks. |
| 5–6             | Site supervisor starts primary and standby from the same deployment set.                                                                                  |
| 7–8             | `operate` records GET `/ready` for authority and GET `/health` for process and fleet state.                                                               |
| 9–10            | Site runs the approved deployment workload through ingress and checks dashboard collection and paging thresholds; `operate` retains results.              |
| 11–12           | `operate` coordinates the policy-permitted engine lifecycle and site-supervised router failover drills below.                                             |
| 13              | Site opens client admission after the complete checklist passes; record the ingress handoff.                                                              |

| Procedure | Input and operation | Evidence | Hold condition |
| --- | --- | --- | --- |
| [Start router pair](operate/01-Start-Routers.md) | Passing deployment set, shared lease and compatible handoff; site starts both `narwhal-serve` processes. | Version manifest, supervisor receipts, `/health`, `/ready` and lease observations in `procedures/start-pair/`. | Handoff mismatch, absent lease or conflicting readiness. |
| [Monitor](operate/02-Monitor.md) | Router and engine targets; GET `/health`, `/ready`, `/narwhal/state`, `/metrics`; inspect journals and dashboard. | Timestamped state and alert context in `procedures/monitor/`. | Incomplete observation or unavailable target. |
| [Individual restart](operate/03-Restart-Engines.md#7-restart-one-engine) | Individual policy and engine ID; POST drain, wait for `ready_to_stop`, replace process, POST readmit. | Lifecycle responses, new identity and readmission result in `procedures/engine-restart/`. | Uncertain drain or HTTP 409 on readmission. |
| [Wave restart](operate/03-Restart-Engines.md#8-restart-an-engine-wave) | Whole-wave policy; POST wave drain, wait for readiness withdrawal, replace all engines, POST wave readmit. | Lifecycle state, supervisor receipts, attestations and KV ring in `procedures/wave-restart/`. | A member fails validation. |
| [Upgrade or rollback](operate/04-Upgrade-and-Validate.md#10-upgrade-and-rollback) | Complete current, candidate and rollback sets; site follows the handoff-compatible or maintenance sequence. | Set identities, readiness, lease epochs and receipts in `procedures/upgrade/`. | Mixed sets or failed postcondition. |
| [Release drills](operate/04-Upgrade-and-Validate.md#11-validate-every-release) | Idle fleet, production supervisor and load balancer; run engine restart and router failover. | Drain, readmission, lease, role, counter and load-balancer evidence in `procedures/release-drills/`. | A drill pass condition fails. |
| [Diagnose](Diagnostic-Bundles.md) | Router URL, fresh output directory and selected sources; run `narwhal diagnostics collect --format json`. | Command result, bundle manifest and source hashes in `diagnostics/`. | Exit 2 or 4 fails collection; exit 3 records partial coverage. |

For each site-supervised action, the Operator sends the target and approved parameters to the site's process adapter or produces a human handoff. The site performs SSH access, installation, process changes, and load-balancer updates and returns a receipt. The Operator verifies router admission through `/ready` and records the supervisor receipt alongside the observed state.

## Private files and export

Store `.env`, credentials, API keys, host-key records, fleet URLs, request content, journals, raw logs, and unrestricted configuration in the private site environment. The host adapter resolves credentials at invocation time. Exported evidence uses stable aliases for private addresses and paths. Review raw subprocess output and site logs before sharing; native command results redact their documented credential fields.

The [diagnostic collector](Diagnostic-Bundles.md#content-policy) uses its default request-content exclusion and credential redaction policy. Select `--include-request-content` when the incident scope calls for those artifacts. Inspect the manifest for partial collection and review application-specific free text before export. Export reviewed excerpts for the approved audience and retain source bundles under the site's policy. Store production evidence in the private run directory.

## Implementation ownership

The plugin implements workflow orchestration and evidence indexing. Host and site adapters implement package installation, permissions, approval enforcement, and supervisor handoff receipts. Narwhal's finite commands, lifecycle routes, and diagnostic collector provide the runtime operations. If a workflow lacks an operation, the implementer records the affected procedure, inspected interface, required state transition, idempotency rule, and owning package.
