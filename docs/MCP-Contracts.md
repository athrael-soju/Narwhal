# MCP fleet operations contract (proposed)

This specification defines how an MCP client selects a Narwhal deployment,
requests an operation and inspects its result. Use it when implementing the
server or an operation adapter.

The [MCP server command](cli/MCP.md) is **unreleased work** on the MCP milestone
branch. It validates a local registry and exposes `target_list`,
`config_inspect`, `config_validate`, `fleet_status`, `diagnostics_collect` and
`artifact_read`. These tools enforce target inspection grants, bound collection
and return redacted results. The remaining tools, deployment adapter and shared
operation runner below remain **planned behaviour**.

The distribution remains `narwhal-inference`, with an optional `mcp`
extra and a `narwhal-mcp` executable. The server runs on a Linux management
workstation over stdio. The user's agent client supplies the model and
conversation. Under this contract, the server delegates operations to Narwhal's
CLI, documented HTTP endpoints and a site adapter. It has no model dependency.

## Contract map

| Contract | Use it to define |
| --- | --- |
| [Registration and permissions](mcp/Registration.md) | How the operator binds target IDs to local inputs, endpoints and allowed actions. |
| [Tools and results](mcp/Tools.md) | Tool arguments, results, errors and artifact access. |
| [Plans and deployment adapter](mcp/Deployment.md) | How the executor records deployment inputs and checks each gate. |
| [Operations and recovery](mcp/Operations.md) | What the executor persists and how it resumes interrupted work. |
| [Worked cases](mcp/Worked-Cases.md) | Expected results for deployment, failure, duplicate submission and recovery. |

These pages define the version 1 contracts and identify their availability.
Fields are required unless their entry specifies an omission default. `null`
is allowed only where stated. Input objects reject unknown fields. Output
readers tolerate added fields within a supported document version, while
preserving unknown diagnostic codes.

## Existing behaviour and new work

The configuration and diagnostic tools invoke finite installed commands with
[versioned command results](Command-Results.md). Configuration inspection,
diagnostic collection, engine launch, local development, profiling and preflight
retain their documented behaviour. The inspection adapters select `--format
json` and preserve each command's outcome and evidence after redaction. They
read registered local inputs and router endpoints; only the planned execution
tools can request deployment changes or active measurements.

The [HTTP inspection routes](HTTP-API.md) supply live router state. Site tooling
currently coordinates [Gates A–G](Deploy.md) and
[monitoring startup](observability/01-Start-and-Verify.md). The proposed adapter
makes those workflows callable through the same operation runner used by MCP
and management CLI entry points. Existing instance locks and stage records do
not yet provide that shared runner or complete fleet resumption.

Prometheus continues to scrape router and engine metrics. The planned
observability tools configure monitoring, inspect its health and query those
metrics within fixed limits. Host tools collect registered inventory and log
sources. Website crawling and content ingestion are outside this specification.

## Ownership

| Area | Owner |
| --- | --- |
| Stdio transport, MCP discovery, schemas and response mapping | `src/narwhal/mcp/` |
| Target registration, plans, operation persistence, coordination and site adapter interface | `src/narwhal/deployment/`, extended by this milestone |
| Local instance lifecycle and its existing ownership checks | `src/narwhal/dev/` |
| Fleet loading and effective configuration | `src/narwhal/config/` |
| Diagnostic bundles and preflight | `src/narwhal/diagnostics/` |
| Profiles, probes and fit validation | `src/narwhal/profiling/` |
| Engine protocols and attestation | `src/narwhal/engines/` |
| Serving lifecycle, state, leases and readmission | Existing `serving/` and `runtime/` owners |
| Metric and journal contracts | `src/narwhal/observability/` |
| SSH access, source installation, network preparation and remote supervision | Site adapter, using the deployment interface |

Narwhal's controller retains authority over live role allocation. The management
runner requests supported lifecycle actions and verifies their results. It must
not write router state or force lease ownership. See
[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md)
when choosing the package for an implementation change.

## Availability and validation

The server must advertise only tools whose operations and permission checks are
implemented. Protocol tests use fixture adapters to check dispatch and result
mapping; those adapters are absent from the installed tool list. The
[server command reference](cli/MCP.md) records the SDK constraint and the
client/protocol combinations that have been checked.

The installed contract manifest includes `narwhal.management-registry` version
1 and `narwhal.management-result` version 1 because the registry reader and
result adapter implement those formats. Deployment plan and operation document
names remain outside the manifest until their readers and writers ship.

[MCP Fleet Operations v1](https://github.com/athrael-soju/Narwhal/issues/147)
tracks delivery and qualification. The inspection tools have no authority to
start engines or qualify a fleet. The proposed runner has not passed GPU or
recovery qualification.
