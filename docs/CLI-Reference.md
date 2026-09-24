# Narwhal CLI reference

`narwhal-inference` installs the five commands below. Each accepts `-h` and `--help`, and resolves relative paths from the process working directory.

Use the installed `narwhal-*` commands in deployment scripts because internal Python module paths can change between releases. `python -m narwhal.cli` also starts a router.

| Command                             | Purpose                                                                 |
| ----------------------------------- | ----------------------------------------------------------------------- |
| [`narwhal-attest`](cli/Attest.md)   | Serve engine identity and attestation data for one vLLM engine          |
| [`narwhal-serve`](cli/Serve.md)     | Run a Narwhal router                                                    |
| [`narwhal-profile`](cli/Profile.md) | Measure engine behaviour and write the profile store used by the router |
| [`narwhal-check`](cli/Check.md)     | Run deployment preflight gates                                          |
| [`narwhal-observe`](cli/Observe.md) | Start the canonical Prometheus and Grafana stack for a fleet            |
