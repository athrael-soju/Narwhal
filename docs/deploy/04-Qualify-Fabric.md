# Gate D: Prove the transfer fabric against the serving cache

Keep representatives idle while measuring each directed link against a budget derived from their resolved cache layout.

## Build the source budget

The initial trial assumes one remote handoff per second, 1,024 prompt tokens per handoff, burst size one, one-second transfer budget, and 25% bandwidth headroom. Apply the full handoff rate independently to every candidate directed edge.

Calculate:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate \
  --model-config "$NARWHAL_MODEL_DIR/config.json" \
  --launch-config "$NARWHAL_ENGINE_LAUNCH_CONFIG" \
  --runtime-layout "$ENGINE_RUN/cache-layout.json" \
  --prompt-tokens 1024 --handoffs-per-s 1 --burst 1 \
  --transfer-budget-s 1 --headroom 1.25 \
  --out "$FABRIC_RUN/budget.json"
```

For every layer and TP rank, page demand is:

```text
ceil(prompt_tokens / block_tokens) + extra_blocks
```

The calculator counts the complete prompt for full attention and MLA, boundary state plus speculative/checkpoint slots for Mamba, and a boundary page for windowed attention. It sums padded page bytes across every layer and TP rank in the resolved runtime layout, so the source budget covers the complete padded cache even when the connector transfers a subset.

Required link rate is:

```text
8 * payload_bytes *
max(handoffs_per_second, burst_handoffs / transfer_budget_seconds) *
headroom / 1e9
```

The result is decimal Gbit/s. Verify one common layout across TP ranks:

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

For each representative, retain `FABRIC_RUN`, `ENGINE_RUN`, budget digest, `required_gbps`, resolved layout, and cache-group signature. Its budget applies to every outgoing edge from roles in that group.

If a role uses another host's representative budget, create a local comparison budget containing the required rate and representative digest:

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

Use a fresh `FABRIC_RUN` for every repeated comparison.

## Bind each sample to a directed route

Begin with engine 1 to engine 2. In both role shells:

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

Use `ip -4 route get` or `ip -6 route get` when needed. Each path must select `NARWHAL_FABRIC_INTERFACE` and the discovered source address. Copy the destination's exact one-line reverse route into `$EDGE_PREFIX.destination-route.txt` on the source. Route, source address, and interface must be correct before bandwidth measurement.

`TEST_PORT` is temporary. Check it on both hosts, permit traffic only between selected fabric addresses, and choose another port if occupied. Existing listeners and firewall rules stay intact.

## Measure `ucx_tcp`

Install `iperf3` on both hosts if required:

```bash
sudo apt-get install iperf3
```

Record `iperf3 --version` from both ends.

Destination:

```bash
iperf3 --server --bind "$DEST_IP" --port "$TEST_PORT"
```

Source, using one TCP stream per TP rank, omitting the first three seconds and measuring ten:

```bash
export FABRIC_STREAMS="$(python3 -c 'import json,os; print(json.load(open(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]))["tensor_parallel_size"])')"
export EDGE_SAMPLE="$EDGE_PREFIX.json"
(set -o noclobber; iperf3 --client "$DEST_IP" --bind "$SOURCE_IP" \
  --port "$TEST_PORT" --parallel "$FABRIC_STREAMS" --omit 3 --time 10 \
  --json > "$EDGE_SAMPLE")
```

Record the fingerprint and compare against the source budget:

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

`record-edge` reads `end.sum_received.bits_per_second`. Exit status 0 means budget met, 1 means measured rate is below budget, and 2 means invalid sample. Stop the temporary server after retention, reverse roles, and repeat against the reverse source's budget.

## Measure `ucx_rdma`

Install `perftest` if required:

```bash
sudo apt-get install perftest
```

Record `ib_write_bw --version`. Select each host's HCA and port from `transfer.net_devices`. For `mlx5_0:1`, use `HCA=mlx5_0` and `HCA_PORT=1`.

For RoCE, inspect:

```text
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/ndevs/
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/types/
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gids/
```

Choose the GID index matching configured fabric interface, address, and RoCE mode, then set `GID_INDEX`. Retain the mapping. In the source shell, set `DEST_HCA`, `DEST_HCA_PORT`, and `DEST_GID_INDEX` from the destination's inspected mapping. Native InfiniBand uses the site's active port and GID selection.

Select address-family flags:

```bash
rdma_addr_args=()
case "$DEST_IP" in
  *:*) rdma_addr_args=(--ipv6-addr --ipv6) ;;
esac
```

Destination:

```bash
ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
  -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
  "${rdma_addr_args[@]}" --bind_source_ip "$DEST_IP"
```

Source:

```bash
export EDGE_SAMPLE="$EDGE_PREFIX.txt"
(set -o noclobber; ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
  -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
  "${rdma_addr_args[@]}" --bind_source_ip "$SOURCE_IP" "$DEST_IP" > "$EDGE_SAMPLE")
cat "$EDGE_SAMPLE"
```

Copy `BW average[Gb/sec]` into `MEASURED_GBPS`, then:

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

This test measures one-way RDMA writes between host-memory buffers. Reverse source and destination and repeat. For multi-rail deployments, test every selected HCA port and retain every report; later live NIXL probes determine how the connector uses the rails.

## Complete the matrix and match retained evidence

For `n` distinct engine hosts, qualify all `n * (n - 1)` directed host pairs. The live KV-transfer gate tests local handoff.

Retain source role, destination role, source revision, source and reverse routes, transport, utility version, exact command, budget signature, sample path, and exit status for every edge.

When host assignment, both routes, interfaces, transport, utility version, and measurement parameters match the retained sample, recreate the current link fingerprint and compare it with the corrected budget:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" reuse-edge \
  --link "$CURRENT_EDGE_PREFIX.link.json" \
  --evidence "$RETAINED_EDGE_PREFIX.evidence.json" \
  --sample "$RETAINED_EDGE_PREFIX.json" \
  --budget "$FABRIC_RUN/budget.json" \
  --out "$CURRENT_EDGE_PREFIX.comparison.json"
```

Recalculate the budget from a new `cache-layout.json` when the runtime layout changes, and collect a new directed sample when host assignment, routes, interfaces, transport, utility version, or measurement parameters change.

RDMA samples use `.txt`. For a legacy sample collected before `record-edge`, reconstruct its link record from verified original routes, interfaces, transport, utility version, and command; collect a new directed sample when those inputs cannot be verified.

A link below its source budget keeps the remaining engines idle while you inspect link speed, MTU, retransmissions or RDMA counters, host CPU saturation, and concurrent traffic. After fixing the cause, sample that directed link again; launch the engines when every edge in the matrix passes.

Continue with [Gate E: Expand the fleet and attest the exact live processes](05-Attest.md).
