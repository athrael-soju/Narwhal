# Gate D: Prove the transfer fabric against the serving cache

Before any real KV traffic moves, measure every directed link between engine hosts and compare it with the bandwidth the source engine's cache will need. Keep the fleet running but idle throughout.

## Build the source budget

The first trial assumes one remote handoff per second, 1,024 prompt tokens per handoff, bursts of one, a one-second transfer budget, and 25% bandwidth headroom. Each candidate directed edge has to carry that full rate on its own. Don't divide it between edges.

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate \
  --model-config "$NARWHAL_MODEL_DIR/config.json" \
  --launch-config "$NARWHAL_ENGINE_LAUNCH_CONFIG" \
  --runtime-layout "$ENGINE_RUN/cache-layout.json" \
  --prompt-tokens 1024 --handoffs-per-s 1 --burst 1 \
  --transfer-budget-s 1 --headroom 1.25 \
  --out "$FABRIC_RUN/budget.json"
```

For each layer and TP rank, the number of pages needed is:

```text
ceil(prompt_tokens / block_tokens) + extra_blocks
```

`extra_blocks` depends on the layer type. Full attention and MLA add none, so they count just the prompt's pages. Mamba layers add one page for the boundary state plus the speculative and checkpoint slots. Windowed attention adds one boundary page. The calculator adds up the padded page bytes across every layer and rank in the runtime layout. The budget therefore covers the whole padded cache, even when the connector only sends part of it.

The required link rate, in decimal Gbit/s, is:

```text
8 * payload_bytes *
max(handoffs_per_second, burst_handoffs / transfer_budget_seconds) *
headroom / 1e9
```

One budget per group only works if every TP rank resolved the same layout. Check that, and take the budget's digest:

```bash
python3 - <<'PY_REPRESENTATIVE_BUDGET'
import json
import os
from pathlib import Path

run = Path(os.environ["FABRIC_RUN"])
budget = json.loads((run / "budget.json").read_text())
layout = json.loads((Path(os.environ["ENGINE_RUN"]) / "cache-layout.json").read_text())
names = {rank["kv_cache_layout"] for rank in layout["ranks"]}
if len(names) != 1:
    raise SystemExit("Inspect the differing TP cache layouts before using one group budget.")
print(f"required_gbps={budget['required_gbps']}; kv_cache_layout={names.pop()}")
PY_REPRESENTATIVE_BUDGET
sha256sum "$FABRIC_RUN/budget.json"
```

For each group's representative engine, write down its `FABRIC_RUN`, `ENGINE_RUN`, the budget digest, `required_gbps`, the resolved layout, and the cache-group signature. That budget applies to every outgoing edge from every engine in the group.

When you measure from an engine that isn't its group's representative, you need a local budget file that carries the representative's rate and digest:

```bash
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
export REQUIRED_GBPS='<required_gbps from representative budget.json>'
export REPRESENTATIVE_BUDGET_SHA256='<SHA-256 of representative budget.json>'
python3 - <<'PY_EDGE_BUDGET'
import json
import os
from pathlib import Path

required = float(os.environ["REQUIRED_GBPS"])
if not 0 < required < float("inf"):
    raise SystemExit("Use the positive finite rate from the representative budget.")
digest = os.environ["REPRESENTATIVE_BUDGET_SHA256"]
if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
    raise SystemExit("Use the representative budget's SHA-256 digest.")
path = Path(os.environ["FABRIC_RUN"]) / "budget.json"
with path.open("x") as stream:
    json.dump({"required_gbps": required, "representative_budget_sha256": digest}, stream)
    stream.write("\n")
path.chmod(0o600)
PY_EDGE_BUDGET
```

Create a new `FABRIC_RUN` every time you repeat a comparison.

## Bind each sample to a directed route

Start with engine 1 to engine 2. In both shells:

```bash
export SOURCE_NODE=1 DEST_NODE=2 TEST_PORT=5201
source_var="NARWHAL_NODE_${SOURCE_NODE}_IP"
dest_var="NARWHAL_NODE_${DEST_NODE}_IP"
export SOURCE_IP="${!source_var:?missing source fabric address}"
export DEST_IP="${!dest_var:?missing destination fabric address}"
ss -ltnp "sport = :$TEST_PORT"
```

On the source:

```bash
export EDGE_PREFIX="$FABRIC_RUN/engine-${SOURCE_NODE}-to-engine-${DEST_NODE}"
ip route get "$DEST_IP" from "$SOURCE_IP" | tee "$EDGE_PREFIX.source-route.txt"
```

On the destination:

```bash
ip route get "$SOURCE_IP" from "$DEST_IP"
```

Add `-4` or `-6` after `ip` if you need to force an address family. Both directions should go out through `NARWHAL_FABRIC_INTERFACE` from the discovered source address. Copy the destination's one-line route exactly into `$EDGE_PREFIX.destination-route.txt` on the source. A bandwidth number is only meaningful for a known route, so get the route, source address, and interface right before you measure anything.

`TEST_PORT` is only needed for the test. Check that it's free on both hosts and pick another if it isn't. Allow traffic on it only between the two fabric addresses, and don't disturb existing listeners or firewall rules.

## Measure TCP links (`ucx_tcp`)

For the `ucx_tcp` transport, measure the underlying TCP path with iperf3. Install it on both hosts if it isn't there:

```bash
sudo apt-get install iperf3
```

Record `iperf3 --version` on both ends.

On the destination:

```bash
iperf3 --server --bind "$DEST_IP" --port "$TEST_PORT"
```

On the source, run one TCP stream per TP rank, ignore the first three seconds, and measure for ten:

```bash
export FABRIC_STREAMS="$(python3 -c 'import json,os; print(json.load(open(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]))["tensor_parallel_size"])')"
export EDGE_SAMPLE="$EDGE_PREFIX.json"
(set -o noclobber; iperf3 --client "$DEST_IP" --bind "$SOURCE_IP" \
  --port "$TEST_PORT" --parallel "$FABRIC_STREAMS" --omit 3 --time 10 \
  --json > "$EDGE_SAMPLE")
```

Record the link fingerprint and compare the sample with the source's budget:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" link \
  --source-role "engine-$SOURCE_NODE" --destination-role "engine-$DEST_NODE" \
  --source-address "$SOURCE_IP" --destination-address "$DEST_IP" \
  --source-interface "$NARWHAL_FABRIC_INTERFACE" \
  --destination-interface "$NARWHAL_FABRIC_INTERFACE" \
  --source-route "$EDGE_PREFIX.source-route.txt" \
  --destination-route "$EDGE_PREFIX.destination-route.txt" \
  --transport ucx_tcp --tool-version "$(iperf3 --version | head -n 1)" \
  --test-parameters "{\"parallel\":$FABRIC_STREAMS,\"omit_s\":3,\"duration_s\":10,\"port\":$TEST_PORT}" \
  --out "$EDGE_PREFIX.link.json"
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" record-edge \
  --link "$EDGE_PREFIX.link.json" --sample "$EDGE_SAMPLE" \
  --budget "$FABRIC_RUN/budget.json" --out "$EDGE_PREFIX.evidence.json"
```

`record-edge` reads `end.sum_received.bits_per_second` from the sample. It exits with 0 if the link meets the budget, 1 if it falls short, and 2 if the sample is invalid. Once the sample and its evidence file are saved, stop the iperf3 server, swap the roles, and measure the reverse direction against the reverse source's budget.

## Measure RDMA links (`ucx_rdma`)

Install `perftest` if it isn't there:

```bash
sudo apt-get install perftest
```

Record `ib_write_bw --version`. Take each host's HCA and port from `transfer.net_devices`. For `mlx5_0:1`, that's `HCA=mlx5_0` and `HCA_PORT=1`.

For RoCE, look in:

```text
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/ndevs/
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/types/
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gids/
```

Pick the GID index that matches the fabric interface, address, and RoCE mode, set `GID_INDEX`, and note the mapping. In the source shell, also set `DEST_HCA`, `DEST_HCA_PORT`, and `DEST_GID_INDEX` from what you found on the destination. On native InfiniBand, use your site's active port and GID selection.

Set the address-family flags:

```bash
rdma_addr_args=()
case "$DEST_IP" in
  *:*) rdma_addr_args=(--ipv6-addr --ipv6) ;;
esac
```

On the destination:

```bash
ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
  -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
  "${rdma_addr_args[@]}" --bind_source_ip "$DEST_IP"
```

On the source:

```bash
export EDGE_SAMPLE="$EDGE_PREFIX.txt"
(set -o noclobber; ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
  -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
  "${rdma_addr_args[@]}" --bind_source_ip "$SOURCE_IP" "$DEST_IP" > "$EDGE_SAMPLE")
cat "$EDGE_SAMPLE"
```

Copy the `BW average[Gb/sec]` figure into `MEASURED_GBPS`, then:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" link \
  --source-role "engine-$SOURCE_NODE" --destination-role "engine-$DEST_NODE" \
  --source-address "$SOURCE_IP" --destination-address "$DEST_IP" \
  --source-interface "$NARWHAL_FABRIC_INTERFACE" \
  --destination-interface "$NARWHAL_FABRIC_INTERFACE" \
  --source-route "$EDGE_PREFIX.source-route.txt" \
  --destination-route "$EDGE_PREFIX.destination-route.txt" \
  --transport ucx_rdma --tool-version "$(ib_write_bw --version | head -n 1)" \
  --test-parameters "{\"source_hca\":\"$HCA\",\"source_port\":$HCA_PORT,\"source_gid\":$GID_INDEX,\"destination_hca\":\"$DEST_HCA\",\"destination_port\":$DEST_HCA_PORT,\"destination_gid\":$DEST_GID_INDEX,\"message_bytes\":1048576,\"duration_s\":10,\"port\":$TEST_PORT}" \
  --out "$EDGE_PREFIX.link.json"
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" record-edge \
  --link "$EDGE_PREFIX.link.json" --sample "$EDGE_SAMPLE" \
  --gbps "$MEASURED_GBPS" --budget "$FABRIC_RUN/budget.json" \
  --out "$EDGE_PREFIX.evidence.json"
```

This test measures one-way RDMA writes between host-memory buffers. Swap source and destination and repeat. If the deployment uses several rails, test every selected HCA port and keep every report. How the connector actually spreads traffic across rails only shows up later, in the live NIXL probes.

## Complete the matrix

With `n` distinct engine hosts, there are `n * (n - 1)` directed host pairs, and each one needs a passing sample. Handoffs between engines on the same host aren't measured here. The live KV transfer checks in Gate F cover those.

For every edge, keep:

- the source and destination roles;
- the source revision;
- both route files;
- the transport and the tool version;
- the exact command;
- the budget signature;
- the sample path and exit status.

### Reusing an earlier sample

You don't have to re-measure a link if its host assignment, both routes, both interfaces, the transport, the tool version, and the test parameters are all unchanged. Rebuild the current link fingerprint and compare the old sample with the recalculated budget:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" reuse-edge \
  --link "$CURRENT_EDGE_PREFIX.link.json" \
  --evidence "$RETAINED_EDGE_PREFIX.evidence.json" \
  --sample "$RETAINED_EDGE_PREFIX.json" \
  --budget "$FABRIC_RUN/budget.json" \
  --out "$CURRENT_EDGE_PREFIX.comparison.json"
```

If the runtime layout has changed, recalculate the budget from the new `cache-layout.json` first. If any of the sample's inputs have changed, take a new sample.

For an RDMA edge, pass the retained `.txt` sample to `--sample`. A sample taken before `record-edge` existed has no link record, so you'd need to rebuild one from the original routes, interfaces, transport, tool version, and command. If you can't verify those, take a new sample.

## When a link is too slow

Keep the fleet idle and check the link speed, MTU, TCP retransmissions or RDMA counters, CPU saturation on either host, and any other traffic sharing the path. Once you've found and fixed the cause, measure that direction again.

Next: [Gate E: Attest the live engine processes](05-Attest.md).
