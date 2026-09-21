# Get started

The profiler and preflight check qualify six CPU engine stubs; the router then selects one prefill engine and one decode engine for the completion. The stubs emit deterministic fixture timings so the walkthrough exercises the control path reproducibly.

## Prerequisites

- Linux with Python 3.11 or newer and its `venv` module
- Git, Make, and curl
- Three terminals

## 1. Install Narwhal

```bash
git clone https://github.com/athrael-soju/Narwhal
cd Narwhal
make setup
source .venv/bin/activate
```

Run subsequent commands from the checkout root. Activate `.venv` in each terminal to invoke `narwhal-*` commands by name, or use their `.venv/bin/` paths.

The CPU stubs use the repository's fixture model and loopback endpoints. The [environment setup](07-Configuration.md#environment-variables) provides a copyable example for engine credentials, fleet selection and observability when you connect a GPU fleet.

Use the same settings in all three terminals. The stub fleet needs six consecutive free loopback ports; the router needs one more. Inspect listeners with `ss -ltnp` before starting. If the defaults are occupied, select a free six-port block and a separate router port while existing listeners keep their ports. The generated fleet config and profiles go under ignored `runs/stub/`.

```bash
export STUB_BASE_PORT=8101
export ROUTER_PORT=8000
export STUB_FLEET="runs/stub/${STUB_BASE_PORT}/fleet.json"
```

On a repeat run, the selected directory may already contain `profiles.json`. Before starting the stubs, set `STUB_FLEET` to a new path in all three terminals, for example `export STUB_FLEET="runs/stub/${STUB_BASE_PORT}-run2/fleet.json"`. Keep the existing path only if you intend to replace its profile and raw samples: add `--overwrite` before `&&` in step 3's profile command.

## 2. Start the stub engines

Keep this process running in the first terminal.

```bash
make stub-fleet
```

The stubs listen on `127.0.0.1` from `STUB_BASE_PORT` through the next five ports and serve the model name `stub`. The launcher checks the full port range before starting any process and writes `STUB_FLEET` with matching URLs. Each stub includes a process-bound attestation endpoint.

## 3. Profile and check the fleet

Run these commands in the second terminal. The `&&` runs preflight only after profiling produces fresh evidence. Start the router after preflight reports `all gates pass`.

```bash
.venv/bin/narwhal-profile \
  --fleet "$STUB_FLEET" \
  --prefill-lens 256,512,1024,2048,4096,8192,12288,16384 \
  --prefill-repeats 1 \
  --decode-input-lens 256,1024 \
  --decode-concurrency 1,4 \
  --decode-tokens 8 &&
.venv/bin/narwhal-check --fleet "$STUB_FLEET"
```

The reduced profile writes `profiles.json` beside the generated fleet config. Use the workload-shaped sweep in [Deploy](03-Deploy.md) for real engines.

With the default `STUB_BASE_PORT=8101`, the profiler confirms its output path:

```text
wrote 6 profile(s) to runs/stub/8101/profiles.json
```

The check then reports every gate and exits with:

```text
all gates pass
```

## 4. Start the router

Keep the router running in the second terminal.

```bash
.venv/bin/narwhal-serve --fleet "$STUB_FLEET" --port "$ROUTER_PORT"
```

After loading all six engine profiles, the router emits a startup log with the journal path and run ID.

## 5. Verify the request path

Use the third terminal.

```bash
curl -fsS "http://127.0.0.1:${ROUTER_PORT}/health"
curl -fsS "http://127.0.0.1:${ROUTER_PORT}/ready"
curl -fsS "http://127.0.0.1:${ROUTER_PORT}/v1/completions" \
  -H 'content-type: application/json' \
  -d '{"model":"stub","prompt":"Explain why narwhals have tusks.","max_tokens":32}'
```

Once every engine passes attestation, the router answers `/health` with all six instances and answers `/ready` while admission accepts traffic.

```json
{"status":"ok","instances":6,"available_instances":6}
```

For this non-streaming request, the router buffers the decode engine's token stream, assembles `choices[0].text`, and returns the completed JSON response. The CPU stubs emit `t0` through `t31`; those 32 placeholders confirm the routed completion. Query `/narwhal/state` to inspect placement and accounting.

```bash
curl -fsS "http://127.0.0.1:${ROUTER_PORT}/narwhal/state" | python3 -m json.tool
```

The state response lists every stub under `pools`, and the router appends one request row to `journal.jsonl` beside the generated profile. With the default base port, that is `runs/stub/8101/journal.jsonl`.

Stop the router with Ctrl-C in the second terminal, then stop the stub fleet with Ctrl-C in the first. The profiler and router leave profiles, raw samples, and the append-only journal under `runs/stub/`. A new run directory starts a new journal; reusing the directory appends.

## Where to go next

- [Core concepts](02-Core-Concepts.md) explains request placement and fleet control.
- [Deploy](03-Deploy.md) connects a real engine fleet.
- [Observability](10-Observability.md) adds Prometheus and the Grafana dashboard to a running fleet.
- [Measure a fleet](06-Measure.md) calibrates the same path under load.
- [API and data reference](09-API-and-Data-Reference.md) defines the routes used above.
