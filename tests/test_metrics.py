"""The Prometheus endpoint the fleet has been scraping into a 404."""

from __future__ import annotations

from narwhal.metrics import Histogram, buckets_for, render


def _state(**over):
    base = {
        "served": 178,
        "failed": 2,
        "unserved": 5,
        "pools": {"prefill": ["n1", "n2"], "decode": ["n3", "n4"]},
        "load": {"prefill": 0.4, "decode": 1.2},
        "resident": {"n1": {"prefill": 3, "decode": 0}},
        "flips": [
            {"iid": "n1", "to": "decode", "by": "algorithm2"},
            {"iid": "n1", "to": "prefill", "by": "algorithm1"},
            {"iid": "n2", "to": "decode", "by": "algorithm2"},
        ],
        "flips_refused": [{"at": 1.0, "to": "prefill", "why": "decode load 1.20"}],
    }
    base.update(over)
    return base


def test_the_counters_carry_the_adaptivity_numbers():
    out = render(_state(), Histogram(buckets_for(10.0)), Histogram(buckets_for(0.125)))
    assert "arrow_served_total 178" in out
    assert "arrow_failed_total 2" in out
    assert "arrow_unserved_total 5" in out
    assert "arrow_flip_reversals_total 1" in out, "n1 went decode then prefill"
    assert "arrow_flips_refused_total 1" in out


def test_flips_are_labelled_by_target_and_caller():
    out = render(_state(), Histogram(buckets_for(10.0)), Histogram(buckets_for(0.125)))
    assert 'arrow_flips_total{to="decode",by="algorithm2"} 2' in out
    assert 'arrow_flips_total{to="prefill",by="algorithm1"} 1' in out


def test_pools_and_loads_are_gauges():
    out = render(_state(), Histogram(buckets_for(10.0)), Histogram(buckets_for(0.125)))
    assert 'arrow_pool_instances{role="prefill"} 2' in out
    assert 'arrow_pool_load{role="decode"} 1.2' in out
    assert 'arrow_resident_requests{iid="n1",phase="prefill"} 3' in out


def test_a_histogram_counts_into_its_buckets():
    h = Histogram((0.1, 1.0))
    for v in (0.05, 0.5, 5.0):
        h.observe(v)
    out = "\n".join(h.render("arrow_ttft_seconds", "x"))
    assert 'arrow_ttft_seconds_bucket{le="0.1"} 1' in out
    assert 'arrow_ttft_seconds_bucket{le="1.0"} 2' in out
    assert 'arrow_ttft_seconds_bucket{le="+Inf"} 3' in out
    assert "arrow_ttft_seconds_count 3" in out
    assert "arrow_ttft_seconds_sum 5.55" in out


def test_every_metric_declares_its_type():
    out = render(_state(), Histogram(buckets_for(10.0)), Histogram(buckets_for(0.125)))
    names = {line.split()[2] for line in out.splitlines() if line.startswith("# TYPE")}
    for line in out.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        metric = line.split("{")[0].split()[0]
        base = metric.removesuffix("_bucket").removesuffix("_sum").removesuffix("_count")
        assert base in names, f"{metric} has no TYPE line"


def test_ejections_are_exported_for_alerting():
    """The one availability signal worth an alarm: a fleet watcher must see
    an ejection without reading /arrow/state."""
    from narwhal.metrics import Histogram, buckets_for, render

    state = {"served": 1, "failed": 0, "ejected": ["n4", "n2"], "flips": []}
    text = render(state, Histogram(buckets_for(10.0)), Histogram(buckets_for(0.125)))
    assert "arrow_ejected_instances 2" in text
    assert 'arrow_ejected{iid="n4"} 1' in text
    assert 'arrow_ejected{iid="n2"} 1' in text


def test_a_healthy_fleet_exports_zero_ejected():
    from narwhal.metrics import Histogram, buckets_for, render

    text = render(
        {"served": 0, "failed": 0, "flips": []},
        Histogram(buckets_for(10.0)),
        Histogram(buckets_for(0.125)),
    )
    assert "arrow_ejected_instances 0" in text


def test_buckets_scale_to_the_configured_slo():
    """Quantile resolution lands near the fleet's own targets rather than
    near the shipped example's."""
    from narwhal.metrics import buckets_for

    for slo in (0.06, 3.0, 10.0):
        edges = buckets_for(slo)
        assert list(edges) == sorted(edges)
        assert slo in edges, "one edge sits exactly at the target"
        below = [e for e in edges if e < slo]
        above = [e for e in edges if e > slo]
        assert len(below) > len(above), "most resolution is under the target"


def test_an_aggregated_fleet_reports_both_roles_per_engine():
    """No prefill pool means every engine runs both phases; the metric says
    what the engines do, not what the routing label happens to be."""
    from narwhal.metrics import Histogram, render

    state = _state(pools={"prefill": [], "decode": ["e0", "e1"]})
    out = render(state, Histogram((0.1, 1.0)), Histogram((0.1, 1.0)))
    assert 'arrow_instance_role{iid="e0",role="decode"} 1' in out
    assert 'arrow_instance_role{iid="e0",role="prefill"} 1' in out
    assert 'arrow_instance_role{iid="e1",role="prefill"} 1' in out
