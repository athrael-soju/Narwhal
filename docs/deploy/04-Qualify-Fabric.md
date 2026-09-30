# Gate D: Prove the transfer fabric against the serving cache

Before real KV traffic moves, measure every directed edge between engine hosts and compare it with the bandwidth the source engine's KV cache needs. Keep the fleet running and idle for the whole gate.

## Build the source budget

The first trial assumes one remote handoff per second, 1,024 prompt tokens per handoff, bursts of one, a one-second transfer budget, and 25% bandwidth headroom. Each directed edge must carry the full rate by itself. Do not split it across edges.

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

`extra_blocks` depends on the layer type:

| Layer type | `extra_blocks` |
| --- | --- |
| Full attention, MLA | 0 |
| Mamba | 1 boundary-state page, plus the speculative and checkpoint slots |
| Windowed attention | 1 boundary page |

The calculator sums the padded page bytes of every layer and rank in the runtime layout, so the budget covers the whole padded cache, including any part the connector does not send.

The required link rate, in decimal Gbit/s, is:

```text
8 * payload_bytes *
max(handoffs_per_second, burst_handoffs / transfer_budget_seconds) *
headroom / 1e9
```

One budget per cache group requires every TP rank to resolve the same layout. Confirm that, then record the budget's SHA-256 digest:

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

For each cache group's representative engine, record:

- `FABRIC_RUN` and `ENGINE_RUN`
- the budget digest and `required_gbps`
- the resolved layout and the cache group signature

This budget applies to every outgoing edge from every engine in the group.

On an engine that is not its group's representative, create a local `budget.json` that carries the representative's `required_gbps` and digest:

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

`budget.json` is created exclusively, so create a new `FABRIC_RUN` for each repeated comparison.

## Bind each sample to a directed route

Start with engine 1 to engine 2. Open a shell on the source and one on the destination, and run this in both. Change `SOURCE_NODE` and `DEST_NODE` for other pairs.

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

Add `-4` or `-6` after `ip` to force an address family.

1. Confirm both routes leave through `NARWHAL_FABRIC_INTERFACE` from the discovered source address. A bandwidth number is only meaningful for a known route, so get the route, source address, and interface right before you measure.
2. Copy the destination's one-line route exactly into `$EDGE_PREFIX.destination-route.txt` on the source.

`TEST_PORT` carries test traffic only. The `ss` command above must show it free on both hosts; if it is in use, pick another port. Allow traffic on it between the two fabric addresses only, and leave existing listeners and firewall rules as they are.

## Measure TCP links (`ucx_tcp`)

For the `ucx_tcp` transport, measure the underlying TCP path with iperf3. Install iperf3 on both hosts if missing:

```bash
sudo apt-get install iperf3
```

Record `iperf3 --version` on both ends.

On the destination:

```bash
iperf3 --server --bind "$DEST_IP" --port "$TEST_PORT"
```

On the source, run one TCP stream per TP rank, omit the first 3 s, and measure for 10 s:

```bash
export FABRIC_STREAMS="$(python3 -c 'import json,os; print(json.load(open(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]))["tensor_parallel_size"])')"
export EDGE_SAMPLE="$EDGE_PREFIX.json"
(set -o noclobber; iperf3 --client "$DEST_IP" --bind "$SOURCE_IP" \
  --port "$TEST_PORT" --parallel "$FABRIC_STREAMS" --omit 3 --time 10 \
  --json > "$EDGE_SAMPLE")
```

Create the link record, then compare the sample with the group budget:

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

`record-edge` reads `end.sum_received.bits_per_second` from the sample. Exit codes:

- 0: the edge meets the budget
- 1: the edge is below budget
- 2: the sample is invalid

Stop the iperf3 server, swap source and destination, and repeat for the reverse direction using the budget for the new source's cache group.

## Measure RDMA links (`ucx_rdma`)

Install `perftest` on both hosts if missing:

```bash
sudo apt-get install perftest
```

Record `ib_write_bw --version`. Take each host's HCA and port from `transfer.net_devices`. For `mlx5_0:1`, set `HCA=mlx5_0` and `HCA_PORT=1`.

For RoCE, look in:

```text
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/ndevs/
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/types/
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gids/
```

1. On each host, pick the GID index that matches the fabric interface, address, and RoCE mode, and set `GID_INDEX`.
2. Record which interface, address, and RoCE mode the chosen index maps to.
3. In the source shell, also set `DEST_HCA`, `DEST_HCA_PORT`, and `DEST_GID_INDEX` from the destination's values.

On native InfiniBand, use the site's active port and GID.

For IPv6 fabric addresses, set the IPv6 flags:

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

Read the `BW average[Gb/sec]` value from the report and export it as `MEASURED_GBPS`. Then create the link record and compare:

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

This test measures one-way RDMA writes between host-memory buffers. Swap source and destination and repeat for the reverse direction. If the deployment uses several rails, test every selected HCA port and keep every report. Multi-rail traffic distribution is checked in the live NIXL probes in [Gate F](06-Profile-and-Preflight.md).

## Complete the matrix

With `n` distinct engine hosts, there are `n * (n - 1)` directed host pairs (edges), and each needs a passing sample. Handoffs between engines on the same host are not measured here. The live KV transfer checks in [Gate F](06-Profile-and-Preflight.md) cover them.

For every edge, keep:

- the source and destination roles
- the source revision
- both route files
- the transport and the tool version
- the exact command
- the budget signature
- the sample path and exit status

### Reusing an earlier sample

An earlier sample is reusable when all of these are unchanged:

- the host assignment
- both routes and both interfaces
- the transport and the tool version
- the test parameters

Rebuild the link record for the current setup and compare the retained sample with the current budget:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" reuse-edge \
  --link "$CURRENT_EDGE_PREFIX.link.json" \
  --evidence "$RETAINED_EDGE_PREFIX.evidence.json" \
  --sample "$RETAINED_EDGE_PREFIX.json" \
  --budget "$FABRIC_RUN/budget.json" \
  --out "$CURRENT_EDGE_PREFIX.comparison.json"
```

If the runtime layout has changed, recalculate the budget from the new `cache-layout.json` first. If any listed input has changed, take a new sample.

For an RDMA edge, pass the retained `.txt` sample to `--sample`. A sample taken before `record-edge` existed has no link record, so rebuild one from the original routes, interfaces, transport, tool version, and command. If they cannot be verified, take a new sample.

## When a link is too slow

With the fleet idle, check:

- link speed and MTU
- TCP retransmissions or RDMA counters
- CPU saturation on both hosts
- other traffic sharing the path

After fixing the cause, repeat the measurement for that direction with a new `FABRIC_RUN`.

Next: [Gate E: Attest the live engine processes](05-Attest.md).
