"""§5.3's cost functions, held to their shape over the whole input space.

The unit tests pin points; these pin the properties the algorithm relies on:
monotonicity in the work offered, the meaning of the pair's two slots, and
`meets_slo` agreeing with the slot it reads. Deterministic (`derandomize`)
so CI cannot flake.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from narwhal.monitor import InstanceMonitor
from narwhal.profiler import Profile, ProfileStore
from narwhal.scheduler import SLO, GlobalScheduler, Thresholds
from narwhal.types import Instance, Phase, Request, Role

settings.register_profile("ci", max_examples=50, deadline=None, derandomize=True)
settings.load_profile("ci")

lengths = st.integers(min_value=1, max_value=200_000)


def build() -> tuple[InstanceMonitor, GlobalScheduler]:
    store = ProfileStore(Path(tempfile.mkdtemp()) / "profiles.json")
    mon = InstanceMonitor(profiles=store)
    for k in range(2):
        iid = f"i{k}"
        mon.add(
            Instance(iid=iid, url=f"http://{iid}", role=Role.PREFILL if k == 0 else Role.DECODE)
        )
        store.put(
            Profile(
                iid=iid,
                ttft_a=1e-8,
                ttft_b=1e-3,
                ttft_c=0.0,
                tpot_slope=1.25e-5,
                tpot_intercept=0.0,
            )
        )
    sched = GlobalScheduler(mon, store, SLO(ttft_s=1.0, tpot_s=0.05), Thresholds())
    return mon, sched


@given(a=lengths, b=lengths)
def test_prefill_cost_is_monotone_in_input_length(a: int, b: int):
    """§5.3's T(r, i) grows with the prompt: a longer prompt never prices
    cheaper on the same idle instance."""
    mon, sched = build()
    inst = mon.instances["i0"]
    small, large = sorted((a, b))
    c_small = sched.cost(Request(rid="a", input_len=small), inst)
    c_large = sched.cost(Request(rid="b", input_len=large), inst)
    assert c_small[1] <= c_large[1]


@given(resident=lengths, incoming=lengths)
def test_resident_prefill_work_raises_the_price(resident: int, incoming: int):
    """The queue term: work already on the instance is charged to the next
    request, so an idle twin always prices at or below a loaded one."""
    mon, sched = build()
    idle, loaded = mon.instances["i0"], mon.instances["i1"]
    mon.dispatched("i1", Request(rid="r", input_len=resident))
    req = Request(rid="q", input_len=incoming)
    assert sched.cost(req, idle)[1] <= sched.cost(req, loaded)[1]


@given(resident=lengths, incoming=lengths)
def test_decode_work_pressures_slot_zero_of_a_prefill_cost_only(resident: int, incoming: int):
    """The pair's meaning: for a prefill leg, decode-resident work lands in
    slot 0 (the other phase's pressure, compared first) and never in slot 1
    (this leg's own price). Lexicographic min then prefers the instance not
    busy with the other phase before it compares own prices."""
    mon, sched = build()
    req = Request(rid="q", input_len=incoming)
    before = sched.cost(req, mon.instances["i0"])
    mon.dispatched("i0", Request(rid="d", input_len=resident, phase=Phase.DECODE))
    after = sched.cost(req, mon.instances["i0"])
    assert after[0] > before[0]
    assert after[1] == before[1]


@given(n=lengths)
def test_meets_slo_reads_exactly_the_own_cost_slot(n: int):
    """§5.3: the admission check is the second component against the TTFT
    target, nothing else."""
    mon, sched = build()
    req = Request(rid="q", input_len=n)
    cost = sched.cost(req, mon.instances["i0"])
    assert sched.meets_slo(req, cost) == (cost[1] <= sched.slo.ttft_s)


@given(resident=lengths, incoming=lengths)
def test_decode_cost_is_monotone_in_the_request_length(resident: int, incoming: int):
    """The decode slot prices `D + {r}` against headroom: more length to
    serve never prices cheaper on the same instance."""
    mon, sched = build()
    inst = mon.instances["i1"]
    mon.dispatched("i1", Request(rid="d", input_len=resident, phase=Phase.DECODE))
    small = Request(rid="a", input_len=min(resident, incoming), phase=Phase.DECODE)
    large = Request(rid="b", input_len=max(resident, incoming), phase=Phase.DECODE)
    assert sched.cost(small, inst)[1] <= sched.cost(large, inst)[1]
