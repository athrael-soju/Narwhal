# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through [GitHub security advisories](https://github.com/athrael-soju/Narwhal/security/advisories/new), or by email to athrael.soju@gmail.com. Include the affected version or commit, reproduction steps, and the impact you observe. Maintainers review each report, develop a fix, and publish an advisory with the release that carries it.

## Scope

This policy covers the `narwhal-inference` package and the `narwhal-*` commands. Operators manage fleet nodes, engine images, site automation and infrastructure credentials. Report defects in third-party components such as vLLM and NIXL to their upstream projects.

Store infrastructure credentials in the site's secret backend. `.gitignore` rules exclude `config/fleet.json`, working fleet configs under `config/fleet.*.json` and the repository `.env`; `make publication` then scans the Git index for private files and key material before a change ships.

## Supported versions

Fixes land on `main` and ship in the next release. Run the latest release to carry every published fix.
