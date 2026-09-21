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

This CPU walkthrough needs no credentials or `.env` file. The optional [environment setup](07-Configuration.md#environment-variables) provides a copyable example for engine credentials, fleet selection and observability when you connect a GPU fleet.

## 2. Start the stub engines

Keep this process running in the first terminal.

```bash
make stub-fleet
```

The stubs listen on ports 8101 through 8106 and serve the model name `stub`. Each stub includes a process-bound attestation endpoint.

## 3. Profile and check the fleet

Run these commands in the second terminal.

```bash
.venv/bin/narwhal-profile \
  --fleet config/fleet.stub.json \
  --prefill-lens 256,512,1024,2048,4096,8192,12288,16384 \
  --prefill-repeats 1 \
  --decode-input-lens 256,1024 \
  --decode-concurrency 1,4 \
  --decode-tokens 8
.venv/bin/narwhal-check --fleet config/fleet.stub.json
```

The reduced profile writes `runs/stub/profiles.json`. Use the workload-shaped sweep in [Deploy](03-Deploy.md) for real engines.

The profiler confirms its output path:

```text
wrote 6 profile(s) to runs/stub/profiles.json
```

The check then reports every gate and exits with:

```text
all gates pass
```

## 4. Start the router

Keep the router running in the second terminal.

```bash
.venv/bin/narwhal-serve --fleet config/fleet.stub.json --port 8000
```

After loading all six engine profiles, the router emits a startup log with the journal path and run ID.

## 5. Verify the request path

Use the third terminal.

```bash
curl -s http://localhost:8000/health
curl -fsS http://localhost:8000/ready
curl -s http://localhost:8000/v1/completions \
  -H 'content-type: application/json' \
  -d '{"model":"stub","prompt":"Explain why narwhals have tusks.","max_tokens":32}'
```

Once every engine passes attestation, the router answers `/health` with all six instances and answers `/ready` while admission accepts traffic.

```json
{"status":"ok","instances":6,"available_instances":6}
```

For this non-streaming request, the router buffers the decode engine's token stream, assembles `choices[0].text`, and returns the completed JSON response. Query `/narwhal/state` to inspect the placement and accounting after completion.

```bash
curl -s http://localhost:8000/narwhal/state | python3 -m json.tool
```

The state response lists every stub under `pools`, and the router appends one request row to `runs/stub/journal.jsonl`.

Stop the router with Ctrl-C in the second terminal, then stop the stub fleet with Ctrl-C in the first. The profiler and router leave profiles and journals under `runs/stub/` for the next run.

## Where to go next

- [Core concepts](02-Core-Concepts.md) explains request placement and fleet control.
- [Deploy](03-Deploy.md) connects a real engine fleet.
- [Observability](10-Observability.md) adds Prometheus and the Grafana dashboard to a running fleet.
- [Measure a fleet](06-Measure.md) calibrates the same path under load.
- [API and data reference](09-API-and-Data-Reference.md) defines the routes used above.
