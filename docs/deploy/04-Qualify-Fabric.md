# Gate D: Prove the transfer fabric against the serving cache

Measure each directed host edge against its source cache group's budget while the fleet is idle.

## Build the source budget

The initial trial uses these parameters:

- One remote KV handoff per second.
- 1,024 prompt tokens per handoff.
- A burst size of one.
- A one-second transfer budget.
- 25% bandwidth headroom.
- Each outgoing directed edge from the cache group's roles carries the full handoff rate.

1. In the representative's engine-role shell, calculate the budget:

    ```bash
    python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate \
      --model-config "$NARWHAL_MODEL_DIR/config.json" \
      --launch-config "$NARWHAL_ENGINE_LAUNCH_CONFIG" \
      --runtime-layout "$ENGINE_RUN/cache-layout.json" \
      --prompt-tokens 1024 --handoffs-per-s 1 --burst 1 \
      --transfer-budget-s 1 --headroom 1.25 \
      --out "$FABRIC_RUN/budget.json"
    ```

2. Verify that every tensor parallel (TP) rank shares one cache layout:

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

3. For each representative, retain:

    - `FABRIC_RUN`
    - `ENGINE_RUN`
    - The budget digest
    - `required_gbps`
    - The resolved layout
    - The cache-group signature

4. For each role that uses another host's representative budget, create a local comparison budget from the representative's `required_gbps` and budget digest:

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

5. Create a new `FABRIC_RUN` for each repeat comparison.

Page demand per layer and TP rank is `ceil(prompt_tokens / block_tokens) + extra_blocks`, with extra blocks set by attention type:

| Attention type | Blocks counted |
| --- | --- |
| Full attention and multi-head latent attention (MLA) | The complete prompt |
| Mamba | The complete prompt plus boundary state, speculative slots, and checkpoint slots |
| Windowed attention | The complete prompt plus a boundary page |

Payload per handoff is page demand times padded page bytes, summed over every layer and TP rank in the resolved runtime layout.

The required link rate, in decimal Gbit/s, is:

```text
8 * payload_bytes *
max(handoffs_per_second, burst_handoffs / transfer_budget_seconds) *
headroom / 1e9
```

## Bind each sample to a directed route

The route checks accept `ip route get`, `ip -4 route get`, or `ip -6 route get`.

1. In both engine-role shells, run the edge variable setup and `TEST_PORT` listener check:

    ```bash
    export SOURCE_NODE=1 DEST_NODE=2 TEST_PORT=5201
    source_var="NARWHAL_NODE_${SOURCE_NODE}_IP"
    dest_var="NARWHAL_NODE_${DEST_NODE}_IP"
    export SOURCE_IP="${!source_var:?missing source fabric address}"
    export DEST_IP="${!dest_var:?missing destination fabric address}"
    ss -ltnp "sport = :$TEST_PORT"
    ```

2. When `ss` shows a listener on either host, pick another `TEST_PORT`.
3. Allow `TEST_PORT` traffic between the two fabric addresses for the test.
4. Record the route on the source:

    ```bash
    export EDGE_PREFIX="$FABRIC_RUN/engine-${SOURCE_NODE}-to-engine-${DEST_NODE}"
    ip route get "$DEST_IP" from "$SOURCE_IP" | tee "$EDGE_PREFIX.source-route.txt"
    ```

5. Check the reverse route on the destination:

    ```bash
    ip route get "$SOURCE_IP" from "$DEST_IP"
    ```

6. Copy the destination's exact one-line reverse route into `$EDGE_PREFIX.destination-route.txt` on the source.
7. Confirm that each route selects `NARWHAL_FABRIC_INTERFACE` and the discovered source address.

## Measure `ucx_tcp`

1. Install `iperf3` on each host that is missing it:

    ```bash
    sudo apt-get install iperf3
    ```

2. Record `iperf3 --version` on both hosts.
3. On the destination, start the server:

    ```bash
    iperf3 --server --bind "$DEST_IP" --port "$TEST_PORT"
    ```

4. On the source, run the client with one TCP stream per TP rank:

    ```bash
    export FABRIC_STREAMS="$(python3 -c 'import json,os; print(json.load(open(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]))["tensor_parallel_size"])')"
    export EDGE_SAMPLE="$EDGE_PREFIX.json"
    (set -o noclobber; iperf3 --client "$DEST_IP" --bind "$SOURCE_IP" \
      --port "$TEST_PORT" --parallel "$FABRIC_STREAMS" --omit 3 --time 10 \
      --json > "$EDGE_SAMPLE")
    ```

5. On the source, run the link fingerprint and budget check:

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

6. Stop the temporary `iperf3` server.
7. Measure the reverse edge against the new source's budget.

`record-edge` takes the measured `ucx_tcp` rate from `end.sum_received.bits_per_second`.

`record-edge` exit codes:

- 0: the budget is met.
- 1: the measured rate is below the budget.
- 2: the sample is invalid.

## Measure `ucx_rdma`

| Fabric                                    | GID selection                                               |
| ----------------------------------------- | ----------------------------------------------------------- |
| RDMA over Converged Ethernet (RoCE)       | A global identifier (GID) index, selected in steps 4 to 7   |
| Native InfiniBand                         | The site's active port and GID selection                    |

1. Install `perftest` on each host that is missing it:

    ```bash
    sudo apt-get install perftest
    ```

2. Record `ib_write_bw --version`.
3. On each host, set `HCA` and `HCA_PORT` from `transfer.net_devices`, where `mlx5_0:1` gives `HCA=mlx5_0` and `HCA_PORT=1`.
4. For RoCE, inspect:

    ```text
    /sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/ndevs/
    /sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/types/
    /sys/class/infiniband/$HCA/ports/$HCA_PORT/gids/
    ```

5. Set `GID_INDEX` to the index matching the configured fabric interface, address, and RoCE mode.
6. Retain each host's GID mapping.
7. In the source shell, set `DEST_HCA`, `DEST_HCA_PORT`, and `DEST_GID_INDEX` from the destination's inspected mapping.
8. Select the address-family flags:

    ```bash
    rdma_addr_args=()
    case "$DEST_IP" in
      *:*) rdma_addr_args=(--ipv6-addr --ipv6) ;;
    esac
    ```

9. On the destination, start the server:

    ```bash
    ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
      -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
      "${rdma_addr_args[@]}" --bind_source_ip "$DEST_IP"
    ```

10. On the source, run the client:

    ```bash
    export EDGE_SAMPLE="$EDGE_PREFIX.txt"
    (set -o noclobber; ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
      -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
      "${rdma_addr_args[@]}" --bind_source_ip "$SOURCE_IP" "$DEST_IP" > "$EDGE_SAMPLE")
    cat "$EDGE_SAMPLE"
    ```

11. Set `MEASURED_GBPS` to the report's `BW average[Gb/sec]` value.
12. Run the link fingerprint and budget check:

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

13. Measure the reverse edge against the new source's budget.
14. For multi-rail deployments, test every selected HCA port.

## Complete the matrix and match retained evidence

| KV handoff           | Qualified by                                                        |
| -------------------- | ------------------------------------------------------------------- |
| Between engine hosts | This matrix                                                         |
| Within one host      | The [Gate F preflight](06-Profile-and-Preflight.md#run-preflight)   |

For `n` distinct engine hosts, qualify the `n * (n - 1)` directed host pairs.

For every edge, retain:

- The source and destination roles.
- The source revision.
- The source and reverse routes.
- The transport and utility version.
- The exact command.
- The budget digest.
- The sample path and exit status.

Recalculate the budget from the new `cache-layout.json` when the runtime layout changes.

Link inputs:

- The host assignment
- Both routes
- The interfaces
- The transport
- The utility version
- The measurement parameters

| Link inputs    | Action                             |
| -------------- | ---------------------------------- |
| Any changed    | Collect a new directed sample.     |
| All unchanged  | Reuse the retained sample.         |

Reuse a retained sample:

1. Set `CURRENT_EDGE_PREFIX` to the current link record prefix.
2. Set `RETAINED_EDGE_PREFIX` to the retained evidence prefix.
3. Recreate the current link fingerprint with `link`.
4. Compare the retained sample with the recalculated budget:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" reuse-edge \
  --link "$CURRENT_EDGE_PREFIX.link.json" \
  --evidence "$RETAINED_EDGE_PREFIX.evidence.json" \
  --sample "$RETAINED_EDGE_PREFIX.json" \
  --budget "$FABRIC_RUN/budget.json" \
  --out "$CURRENT_EDGE_PREFIX.comparison.json"
```

`--sample` value by transport:

| Transport  | `--sample` value              |
| ---------- | ----------------------------- |
| `ucx_tcp`  | The retained `iperf3` JSON    |
| `ucx_rdma` | The retained `.txt` report    |

`reuse-edge` exit codes:

- 0: the retained sample meets the recalculated budget.
- 1: the retained sample is below that budget.
- 2: the current link fingerprint or the retained sample differs from the recorded evidence.

## Troubleshoot an edge below its budget

When `record-edge` or `reuse-edge` exits 1:

1. Check link speed, MTU, retransmissions or RDMA counters, CPU load, and other traffic.
2. Resample the directed edge until `record-edge` exits 0.

Next: [Gate E: Attest the live engine processes](05-Attest.md).
