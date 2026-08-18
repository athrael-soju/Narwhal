"""The fleet config, and the conversion the manual tells operators to run."""

from __future__ import annotations

import json

import pytest

from narwhal.config import FleetConfig
from narwhal.types import Role


def _narwhal(tmp_path, **over):
    raw = {
        "model": {"served_name": "Qwen/Qwen3-32B"},
        # A fleet may run one node on a different port from the rest.
        "nodes": {
            "1": {"address": "2001:db8:0:1::7001", "port": 8002},
            "2": {"address": "2001:db8:0:2::7002", "port": 8002},
            "3": {"address": "2001:db8:0:3::7003", "port": 8002},
            "4": {"address": "2001:db8:0:4::7004", "port": 8002},
            "5": {"address": "2001:db8:0:5::7005", "port": 8001},
            "6": {"address": "2001:db8:0:6::7006", "port": 8002},
        },
        "opening_split": {"p": [1, 2, 3], "d": [4, 5, 6]},
        "scenario": {"slo": {"ttft_ms": 10000, "tpot_ms": 125}},
    }
    raw.update(over)
    path = tmp_path / "fleet.narwhal.json"
    path.write_text(json.dumps(raw))
    return path


def test_each_node_keeps_its_own_port(tmp_path):
    """A converter that assumed one port would point five entries at one engine."""
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    ports = {e.iid: e.url.rsplit(":", 1)[1] for e in cfg.engines}
    assert ports["n5"] == "8001"
    assert {ports[f"n{k}"] for k in (1, 2, 3, 4, 6)} == {"8002"}


def test_an_ipv6_address_is_bracketed(tmp_path):
    """Unbracketed, `http://2001:db8:0:1::7001:8002` parses 7608 as the port and the
    router talks to the wrong place without erroring."""
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    for e in cfg.engines:
        assert e.url.startswith("http://[")
        assert e.url.endswith("]:8002") or e.url.endswith("]:8001")


def test_an_ipv4_address_is_left_alone(tmp_path):
    path = _narwhal(tmp_path, nodes={"1": {"address": "10.0.0.4", "port": 8000}})
    cfg = FleetConfig.from_fleet_json(path)
    assert cfg.engines[0].url == "http://10.0.0.4:8000"


def test_the_opening_split_becomes_starting_labels(tmp_path):
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    by = {e.iid: e.role for e in cfg.engines}
    assert [by[f"n{k}"] for k in (1, 2, 3)] == [Role.PREFILL] * 3
    assert [by[f"n{k}"] for k in (4, 5, 6)] == [Role.DECODE] * 3


def test_an_absent_split_opens_even(tmp_path):
    path = _narwhal(tmp_path, opening_split={})
    cfg = FleetConfig.from_fleet_json(path)
    assert sum(e.role is Role.PREFILL for e in cfg.engines) == 3


def test_the_slo_pair_converts_from_milliseconds(tmp_path):
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    assert cfg.slo.ttft_s == 10.0
    assert cfg.slo.tpot_s == 0.125


def test_nodes_are_ordered_numerically_not_lexically(tmp_path):
    """ "10" sorts before "2" as a string, which would misalign every label."""
    nodes = {str(k): {"address": f"10.0.0.{k}", "port": 8000} for k in range(1, 12)}
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path, nodes=nodes, opening_split={}))
    assert [e.iid for e in cfg.engines][:3] == ["n1", "n2", "n3"]
    assert cfg.engines[-1].iid == "n11"


def test_a_converted_config_round_trips(tmp_path):
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    out = tmp_path / "fleet.json"
    cfg.save(out)
    back = FleetConfig.load(out)
    assert [(e.iid, e.url, e.role) for e in back.engines] == [
        (e.iid, e.url, e.role) for e in cfg.engines
    ]
    assert back.slo.ttft_s == cfg.slo.ttft_s
    assert back.first_token_timeout_s == cfg.first_token_timeout_s
    assert back.decode_attempts == cfg.decode_attempts


def test_overrides_apply_after_conversion(tmp_path):
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path), monitor_interval_s=2.5)
    assert cfg.monitor_interval_s == 2.5


def test_a_repeated_instance_id_is_refused(tmp_path):
    path = tmp_path / "dupe.json"
    path.write_text(
        json.dumps(
            {
                "model": "m",
                "engines": [{"iid": "n1", "url": "http://a:1"}, {"iid": "n1", "url": "http://b:1"}],
                "slo": {"ttft_s": 1, "tpot_s": 1},
            }
        )
    )
    with pytest.raises(ValueError, match="repeats an instance id"):
        FleetConfig.load(path)


def test_panic_ratio_parses_validates_and_saves(tmp_path):
    """The panic-bypass knob: 0 is off (the Arrow paper's behaviour), values
    between 0 and 1 would bypass constantly and are refused by name."""
    body = {
        "model": "m",
        "engines": [
            {"iid": "a", "url": "http://a", "role": "prefill"},
            {"iid": "b", "url": "http://b", "role": "decode"},
        ],
        "slo": {"ttft_s": 1.0, "tpot_s": 0.05},
        "thresholds": {"panic_ratio": 2.0},
    }
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(body))
    cfg = FleetConfig.load(path)
    assert cfg.thresholds.panic_ratio == 2.0

    out = tmp_path / "roundtrip.json"
    cfg.save(out)
    assert FleetConfig.load(out).thresholds.panic_ratio == 2.0

    body["thresholds"]["panic_ratio"] = 0.5
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="panic_ratio"):
        FleetConfig.load(path)


def test_an_unknown_admission_policy_is_named(tmp_path):
    raw = {
        "model": "m",
        "engines": [{"iid": "e0", "url": "http://e0"}],
        "slo": {"ttft_s": 10.0, "tpot_s": 0.125},
        "admission": "mood",
    }
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="admission"):
        FleetConfig.load(path)


def test_a_zero_move_cap_is_named(tmp_path):
    from narwhal.scheduler import SLO

    cfg = FleetConfig(
        model="m",
        engines=[],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
        replace_per_pass=0,
    )
    with pytest.raises(ValueError, match="replace_per_pass"):
        cfg.validate()


def test_the_two_prefix_games_are_refused_together(tmp_path):
    """The affinity ablation and the shipped cooperative term: one question, two answers,
    measured apart. Enabling both would attribute one to the other."""
    path = _narwhal(tmp_path)
    cfg = FleetConfig.from_fleet_json(path)
    cfg.prefill_affinity = True
    cfg.prefix_coop = True
    with pytest.raises(ValueError, match="must run alone"):
        cfg.validate()


def test_prefix_coop_parses_validates_and_saves(tmp_path):
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    assert cfg.prefix_coop is False
    assert cfg.prefix_halflife_s == 60.0

    cfg.prefix_coop = True
    cfg.prefix_halflife_s = 45.0
    out = tmp_path / "saved.json"
    cfg.save(out)
    back = FleetConfig.load(out)
    assert back.prefix_coop is True
    assert back.prefix_halflife_s == 45.0

    cfg.prefix_halflife_s = 0.0
    with pytest.raises(ValueError, match="prefix_halflife_s"):
        cfg.validate()


# -- role pinning ------------------------------------------------------


def _pinned_cfg(tmp_path, *, pin_iid="n1", min_prefill=2):
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    for e in cfg.engines:
        e.pin = e.iid == pin_iid
    cfg.min_prefill = min_prefill
    return cfg


def test_pin_and_min_prefill_roundtrip(tmp_path):
    """A pinned engine and the floor survive save/load, and pinning is opt-in:
    an engine that never asked stays unpinned."""
    cfg = _pinned_cfg(tmp_path)
    out = tmp_path / "roundtrip.json"
    cfg.save(out)
    loaded = FleetConfig.load(out)
    assert {e.iid: e.pin for e in loaded.engines} == {f"n{k}": (k == 1) for k in (1, 2, 3, 4, 5, 6)}
    assert loaded.min_prefill == 2
    # The serialized form only names `pin` where it is set, so a config that
    # pins nothing is byte-identical to one written before the feature.
    doc = json.loads(out.read_text())
    assert [("pin" in e) for e in doc["engines"]] == [True, False, False, False, False, False]


def test_defaults_pin_nothing_and_floor_at_one(tmp_path):
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    assert all(not e.pin for e in cfg.engines)
    assert cfg.min_prefill == 1


def test_pinned_engines_validate_at_the_default_floor(tmp_path):
    """Any set of pinned engines, up to the whole fleet, validates with
    min_prefill at the default of 1."""
    for pins in (["n1"], ["n1", "n2"], [f"n{k}" for k in (1, 2, 3, 4, 5, 6)]):
        cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
        for e in cfg.engines:
            e.pin = e.iid in pins
        cfg.min_prefill = 1
        cfg.validate()


def test_a_floor_that_leaves_no_decode_engine_is_refused(tmp_path):
    cfg = FleetConfig.from_fleet_json(_narwhal(tmp_path))
    cfg.min_prefill = 6
    with pytest.raises(ValueError, match="leaves no decode engine"):
        cfg.validate()
