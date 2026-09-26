# MCP fleet operations contract (proposed)

This specification defines the management interfaces for implementers of
[MCP Fleet Operations v1](https://github.com/athrael-soju/Narwhal/issues/147).
It is the design deliverable for
[issue #148](https://github.com/athrael-soju/Narwhal/issues/148).
The MCP server, registration format, tools and operation runner described here
are **planned behaviour**. They are not available in the current package.

The proposed distribution remains `narwhal-inference`, with an optional `mcp`
extra and a `narwhal-mcp` executable. The server runs on a Linux management
workstation over stdio. The user's agent client supplies the model and
conversation. The server delegates operations to Narwhal's CLI, documented
HTTP endpoints and a site adapter; it has no model dependency.

## Contract map

| Contract | Implementer task |
| --- | --- |
| [Registration and permissions](mcp/Registration.md) | Resolve targets, recipes, endpoints and capabilities without accepting arbitrary host paths or commands from tools. |
| [Tools and results](mcp/Tools.md) | Implement the complete tool catalogue, typed arguments, limits, error mapping and artifact access. |
| [Plans and deployment adapter](mcp/Deployment.md) | Prepare immutable plans, check prerequisites and execute the existing deployment gates. |
| [Operations and recovery](mcp/Operations.md) | Persist execution, coordinate concurrent work, reconcile interruption and retain outcomes. |
| [Worked cases](mcp/Worked-Cases.md) | Check the contracts against success, failure, duplicate submission, cancellation and recovery. |

These pages form one specification. Field tables define the proposed version 1
contracts; examples illustrate them. Fields are required unless their entry
specifies an omission default. `null` is allowed only where stated. Input
objects reject unknown fields. Output readers tolerate added fields within a
supported document version, while preserving unknown diagnostic codes.

## Existing behaviour and new work

Finite installed commands already support
[versioned command results](Command-Results.md). Configuration inspection,
diagnostic collection, engine launch, local development, profiling and preflight
retain their documented semantics. The MCP adapter selects `--format json`
where supported and preserves the command's outcome and evidence.

The [HTTP inspection routes](HTTP-API.md) supply live router state. Site tooling
currently coordinates [Gates A–G](Deploy.md) and
[monitoring startup](observability/01-Start-and-Verify.md). The proposed adapter
makes those workflows callable through the same operation runner used by MCP
and management CLI entry points. Existing instance locks and stage records do
not yet provide that shared runner or complete fleet resumption.

The scraping scope recorded in #148 is Prometheus router and engine metrics,
bounded host inventory and selected logs. Prometheus owns periodic scraping.
The MCP server configures and inspects that collection and issues bounded
queries. Website crawling and content ingestion are outside this specification.

## Ownership

| Area | Owner |
| --- | --- |
| Stdio transport, MCP discovery, schemas and response mapping | Proposed `src/narwhal/mcp/` |
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
runner requests supported lifecycle actions and verifies their results.
It does not write router state, force lease ownership or implement a second
role controller. See [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md)
for the package ownership map.

## Availability and validation

Issue #149 implements the server; #150–#155 implement its operations and
execution boundaries. Issue #156 owns assembled package and live fleet
qualification. This design specifies behaviour to test, without claiming that
the proposed runner has passed GPU or recovery qualification.

Each implementation must advertise only tools whose dependencies and contracts
are implemented. SDK selection and verified client/protocol compatibility
belong to #149. Proposed document names must enter the installed contract
manifest only when their readers and writers ship.
