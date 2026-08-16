# Security

## The stance

The router authenticates nothing by default and terminates no TLS, by
design at 0.x: it is built to sit on a trusted fabric behind your own
ingress, and [docs/Deploy.md](docs/Deploy.md) says so where it tells you to
bind it. The optional tenant keys (`tenants` in the config) are the one
door, for fair-share accounting rather than perimeter security. Reports
that the router is reachable without credentials are the design, not a
vulnerability.

## In scope

- The router crossing a trust boundary it claims to hold: a request body
  reaching anything beyond the configured engines, header forwarding beyond
  the three documented headers, credential material (tenant keys included)
  appearing in logs, journals or `/arrow/state`.
- `narwhal-fleet`: credential handling (`SSHPASS`, key auth) and the deploy
  sentinel refusing directories it did not create.
- Dependency advisories that reach an exposed surface.

## Reporting

Use GitHub's private vulnerability reporting on this repository. A report
lands as a private advisory; expect an acknowledgment within a week. Fixes
ship as ordinary releases with the advisory credited unless you ask
otherwise.
