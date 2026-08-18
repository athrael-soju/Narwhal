"""The traffic console: its command grammar and its recording contract."""

from __future__ import annotations

import json

import pytest

from narwhal.live_bench import PRESETS, Scenario, apply_command
from narwhal.trace import load_trace


def test_every_command_mutates_the_scenario_it_names():
    scn = Scenario()
    apply_command(scn, "rate 2.5", now=0.0)
    assert scn.rate == 2.5
    apply_command(scn, "shape 12000-16000 20-40", now=0.0)
    assert scn.isl == (12000, 16000)
    assert scn.osl == (20, 40)
    apply_command(scn, "mult 3", now=0.0)
    assert scn.mult == 3.0
    apply_command(scn, "prefix on 8000 4", now=0.0)
    assert scn.prefix == (8000, 4)
    apply_command(scn, "prefix off", now=0.0)
    assert scn.prefix is None


def test_a_preset_is_a_complete_shape_not_a_partial_edit():
    scn = Scenario()
    apply_command(scn, "prefix on 8000", now=0.0)
    apply_command(scn, "preset wall", now=0.0)
    assert (scn.isl, scn.osl, scn.mult) == ((12000, 16000), (20, 40), 3.0)
    assert scn.prefix is None, "a preset without shared heads turns them off"
    apply_command(scn, "preset prefix", now=0.0)
    assert scn.prefix == (8000, 8)


def test_a_spike_multiplies_until_its_deadline_and_not_after():
    scn = Scenario(rate=1.0)
    apply_command(scn, "spike 3x 30s", now=100.0)
    assert scn.effective_rate(101.0) == 3.0
    assert scn.effective_rate(131.0) == 1.0


@pytest.mark.parametrize("bad", ["rate", "shape 100", "preset nope", "spike 3 30", "warp 9"])
def test_a_malformed_command_raises_instead_of_driving_something_else(bad):
    with pytest.raises((ValueError, TypeError)):
        apply_command(Scenario(), bad, now=0.0)


def test_every_preset_names_positive_bands():
    for name, p in PRESETS.items():
        assert 0 < p["isl"][0] <= p["isl"][1], name
        assert 0 < p["osl"][0] <= p["osl"][1], name
        assert p["mult"] > 0, name


def test_a_recorded_session_replays_through_the_bench_loader(tmp_path):
    """The recording is the experiment: rows written by the console are
    exactly what `narwhal-bench --trace-file` replays, prefixes included."""
    p = tmp_path / "session.jsonl"
    rows = [
        {"at": 0.0, "input_len": 4000, "output_len": 120},
        {"at": 1.5, "input_len": 9000, "output_len": 60, "prefix_id": 3, "prefix_len": 8000},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert load_trace(p, rate=1.0) == [
        (0.0, 4000, 120, None),
        (1.5, 9000, 60, (3, 8000)),
    ]
