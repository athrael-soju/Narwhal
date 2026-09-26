---
name: narwhal-diagnose
description: Diagnose a failed Narwhal deployment gate or live router incident using command results, router state, and private diagnostic bundles. Use when the operator needs the failing stage, evidence, and next check or repair.
---

# Diagnose a Narwhal fleet

Read the [Operator contract](../../../../docs/Operator-Plugin.md) and the [diagnostic bundle contract](../../../../docs/Diagnostic-Bundles.md) from the approved Narwhal revision. Open the affected [gate](../../../../docs/Deploy.md#deployment-sequence) or [troubleshooting path](../../../../docs/Troubleshoot.md) after the failure is located.

## Locate the failure

1. Obtain incident scope, deployment-set reference, private evidence root, affected router or gate, permitted endpoints and files, time budget, and inclusion policy. Record `blocked` for a missing input needed to inspect the failure.
2. Read the latest gate or procedure `record.json`, root `decision.json`, native `narwhal.command-result`, and relevant supervisor receipt. Verify deployment-set digest, process generation, installed build identity, and schema version before interpreting a result. An incompatible verified build or unknown schema is `unsupported`; preserve its raw evidence.
3. Identify the failing stage from `status`, stable `errors[].code`, and `stage`, `engine`, `field`, or `context` when present. Use diagnostic `message` for context. For `failed_gate`, inspect the named predicate and its artifact. For `degraded`, enumerate checks performed and skipped. For `error` or `interrupted`, retain partial artifacts, inspect current state and receipts, and establish effects before any retry.
4. Select the smallest source set that can distinguish the plausible causes. The [runbook recovery index](../../../../docs/Deploy.md#evidence-and-recovery-index) maps each gate to its evidence. For a live router incident, use the [router signals](../../../../docs/Troubleshoot.md#router-admission-and-lifecycle-signals) to choose the next endpoint or file.

`diagnose` uses `observe` and `evidence-write`. It may request a bounded read from approved endpoints and files. A diagnostic request alone does not authorize active probes, lifecycle mutations, process control, or broad log collection. When a repair requires another action class, name the owner and hand off to the approved deployment or operating workflow.

## Collect evidence

Use a fresh private output directory for each router. The [collector](../../../../docs/Diagnostic-Bundles.md#source-selection) reads `/health`, `/ready`, `/narwhal/state`, `/narwhal/lifecycle`, and `/metrics`; select `--fleet`, `--run` or `--instance`, and only incident-specific `--artifact` files. Set a bounded `--timeout` and `--source-timeout` within the invocation budget. Run `narwhal diagnostics collect --format json` from the approved installation and retain stdout, stderr, process exit, bundle, and manifest.

Validate the `narwhal.command-result` envelope and status/exit pairing, then validate the `narwhal.diagnostic-bundle` version 1 manifest before reading source outcomes. Collector exit `0` gives a completed collection, `2` reports input or output-path failure, `3` reports partial collection with command status `degraded` and code `collection_partial`, and `4` reports an I/O failure. For exit `3`, preserve the partial bundle, list failed, timed-out, truncated, and excluded sources, and make a fresh attempt only for a missing source needed to answer the incident. A bundle's `success` means its selected sources were collected; it does not establish fleet health.

Use the [manual capture procedure](../../../../docs/Diagnostic-Bundles.md#manual-collection) only when the installed collector is unavailable. Retain each HTTP status with its body. Keep the default request-content exclusion. Select `--include-request-content` only when the approved incident scope requires it. Review free text and application-specific fields before exporting a sanitised excerpt; raw bundles, addresses, credentials, paths, and request content stay in private storage.

Write an attempt under `diagnostics/<attempt-id>/` with the native result, bundle manifest, source hashes, missing-source list, and `record.json`. Use the Operator contract's `narwhal.operator-evidence` version 1 fields and decision meanings. Record partial collection as `degraded`, unknown schema as `unsupported`, and uncertain effects after a deadline or transport loss as `interrupted`.

Report the failing gate or stage and engine when known, the stable result code, supporting private artifact paths, the cause established by the evidence, the next check or repair, its owner, and the verification that will show recovery. If the cause is not established, report the competing causes and the single next observation that separates them before proposing a retry. A partial bundle or degraded native result cannot support a healthy-fleet conclusion.
