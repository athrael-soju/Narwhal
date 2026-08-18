"""The offline upper bound the study's methodology §D asks for."""

from __future__ import annotations

import json

from narwhal.oracle import _best_split, _split_at, fraction_wrong, read, windows
from narwhal.profiler import Profile


def _profile() -> Profile:
    return Profile(
        "i0", ttft_a=2e-8, ttft_b=6e-5, ttft_c=0.005, tpot_slope=3e-6, tpot_intercept=0.012
    )


def test_the_split_follows_the_demand():
    assert _best_split(prefill_s=90.0, decode_s=10.0, n=6) == 5
    assert _best_split(prefill_s=10.0, decode_s=90.0, n=6) == 1
    assert _best_split(prefill_s=50.0, decode_s=50.0, n=6) == 3


def test_both_pools_keep_an_instance():
    """Algorithm 3's `|S| > 1` guard, read as a floor."""
    assert _best_split(prefill_s=1000.0, decode_s=0.0, n=6) == 5
    assert _best_split(prefill_s=0.0, decode_s=1000.0, n=6) == 1
    assert _best_split(prefill_s=0.0, decode_s=0.0, n=6) == 3


def test_the_controllers_split_is_replayed_from_its_flips():
    flips = [
        {"at": 10.0, "to": "decode"},
        {"at": 20.0, "to": "decode"},
        {"at": 30.0, "to": "prefill"},
    ]
    assert _split_at(0.0, 3, flips) == 3
    assert _split_at(15.0, 3, flips) == 2
    assert _split_at(25.0, 3, flips) == 1
    assert _split_at(35.0, 3, flips) == 2


def test_a_prefill_heavy_window_wants_prefill_instances():
    rows = [
        {"arrived": 0.0, "input_len": 16000, "output_len": 30, "tpot_s": 0.02},
        {"arrived": 1.0, "input_len": 16000, "output_len": 30, "tpot_s": 0.02},
    ]
    ws = windows(
        rows, [], _profile(), n_instances=6, opening_prefill=3, tpot_s=0.125, window_s=10.0
    )
    assert len(ws) == 1
    assert ws[0].prefill_s > ws[0].decode_s
    assert ws[0].best_prefill > 3


def test_a_decode_heavy_window_wants_decode_instances():
    rows = [
        {"arrived": 0.0, "input_len": 400, "output_len": 8000, "tpot_s": 0.02},
        {"arrived": 1.0, "input_len": 400, "output_len": 8000, "tpot_s": 0.02},
    ]
    ws = windows(rows, [], _profile(), 6, 3, 0.125, 10.0)
    assert ws[0].decode_s > ws[0].prefill_s
    assert ws[0].best_prefill < 3


def test_fraction_wrong_counts_the_windows_that_differ():
    ws = windows(
        [
            {"arrived": float(k), "input_len": 400, "output_len": 8000, "tpot_s": 0.02}
            for k in range(20)
        ],
        [],
        _profile(),
        6,
        3,
        0.125,
        10.0,
    )
    assert all(w.actual_prefill == 3 for w in ws)
    assert all(w.best_prefill == 1 for w in ws)
    assert fraction_wrong(ws) == 1.0
    assert fraction_wrong([]) == 0.0


def test_reading_a_run_winds_the_flips_back_to_the_opening(tmp_path):
    """The snapshot is taken after the run, so the opening split has to be
    recovered from it."""
    (tmp_path / "j.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"arrived": 0.0, "input_len": 400, "output_len": 100, "tpot_s": 0.02},
                {"arrived": 5.0, "input_len": 400, "output_len": 100, "tpot_s": 0.02},
            ]
        )
    )
    (tmp_path / "s.json").write_text(
        json.dumps(
            {
                "pools": {"prefill": ["n1"], "decode": ["n2", "n3", "n4", "n5", "n6"]},
                "flips": [{"at": 1.0, "to": "decode"}, {"at": 2.0, "to": "decode"}],
            }
        )
    )
    ws = read(tmp_path / "j.jsonl", tmp_path / "s.json", _profile(), 0.125)
    assert ws[0].actual_prefill == 3, "one prefill instance after two P->D means three before"
