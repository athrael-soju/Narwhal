"""The adaptivity metrics the study's methodology §C asks for alongside goodput."""

from __future__ import annotations

import json

from narwhal.report import _bound, _reversals, read_run, render


def test_a_reversal_is_an_engine_flipped_back():
    """§C: "an engine flipped P->D then D->P within N ticks"."""
    assert _reversals([]) == 0
    one_way = [{"iid": "n1", "to": "decode"}, {"iid": "n2", "to": "decode"}]
    assert _reversals(one_way) == 0, "two engines moving the same way is not a reversal"

    flapping = [
        {"iid": "n1", "to": "decode"},
        {"iid": "n1", "to": "prefill"},
        {"iid": "n1", "to": "decode"},
    ]
    assert _reversals(flapping) == 2

    assert _reversals([{"iid": "n1", "to": "decode"}, {"iid": "n1", "to": "decode"}]) == 0


def test_a_miss_names_the_constraint_that_bound_it():
    """§C: "report the fraction meeting only one SLO to attribute which
    constraint binds"."""
    ok = {"output_len": 10, "wanted_len": 10, "ttft_s": 0.1, "tpot_s": 0.01, "error": None}
    assert _bound(ok, 10, 0.125) == "met"
    assert _bound({**ok, "ttft_s": 40.0}, 10, 0.125) == "ttft"
    assert _bound({**ok, "tpot_s": 0.5}, 10, 0.125) == "tpot"
    assert _bound({**ok, "ttft_s": 40.0, "tpot_s": 0.5}, 10, 0.125) == "ttft+tpot"
    assert _bound({**ok, "output_len": 3}, 10, 0.125) == "short"
    assert _bound({**ok, "error": "ReadTimeout"}, 10, 0.125) == "error"


def test_a_refused_row_is_its_own_bucket_not_an_error():
    """Refusal at the door is an admission KPI; bucketed as error it would
    read as a request the fleet destroyed."""
    row = {
        "output_len": 0,
        "wanted_len": 0,
        "ttft_s": None,
        "tpot_s": None,
        "refused": True,
        "error": "refused: cheapest placement prices TTFT at 50.0s",
    }
    assert _bound(row, 10, 0.125) == "refused"


def test_a_run_is_scored_per_arm_and_rate(tmp_path):
    rows = [
        {
            "arrived": 0.0,
            "output_len": 8,
            "wanted_len": 8,
            "ttft_s": 0.2,
            "tpot_s": 0.02,
            "error": None,
        },
        {
            "arrived": 60.0,
            "output_len": 8,
            "wanted_len": 8,
            "ttft_s": 40.0,
            "tpot_s": 0.02,
            "error": None,
        },
    ]
    (tmp_path / "adaptive.0.3.journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (tmp_path / "adaptive.0.3.state.after.json").write_text(
        json.dumps(
            {
                "flips": [
                    {"at": 5.0, "iid": "n1", "to": "decode", "by": "algorithm2"},
                    {"at": 24.0, "iid": "n1", "to": "prefill", "by": "algorithm1"},
                ]
            }
        )
    )

    scored = read_run(tmp_path, ttft_slo=10.0, tpot_slo=0.125)
    assert len(scored) == 1
    r = scored[0]
    assert (r.arm, r.rate) == ("adaptive", 0.3)
    assert r.met == 1
    assert r.total == 2
    assert r.reroles == 2
    assert r.reversals == 1
    assert r.thrash_per_hour == 60.0, "one reversal in a 60 s window"
    assert r.bound["ttft"] == 1
    assert "ttft 1" in render(scored)


def test_an_arm_with_no_state_file_still_scores(tmp_path):
    (tmp_path / "static.0.3.journal.jsonl").write_text(
        json.dumps(
            {
                "arrived": 0.0,
                "output_len": 4,
                "wanted_len": 4,
                "ttft_s": 0.1,
                "tpot_s": 0.01,
                "error": None,
            }
        )
    )
    scored = read_run(tmp_path, 10.0, 0.125)
    assert scored[0].reroles == 0


def test_time_to_adapt_is_the_lag_from_a_phase_boundary():
    """§C: "lag between the load-shift onset and the completed role change"."""
    from narwhal.report import _time_to_adapt

    flips = [{"at": 5.0}, {"at": 24.0}, {"at": 71.0}]
    # Phases of 30 s starting at 0, so boundaries at 30 and 60.
    assert _time_to_adapt(flips, first_arrival=0.0, phase_s=30.0) == [41.0, 11.0]


def test_a_boundary_with_no_flip_after_it_reports_nothing():
    """Never adapting is not adapting instantly."""
    from narwhal.report import _time_to_adapt

    assert _time_to_adapt([{"at": 5.0}], first_arrival=0.0, phase_s=30.0) == []
    assert _time_to_adapt([], first_arrival=0.0, phase_s=30.0) == []
    assert _time_to_adapt([{"iid": "n1"}], first_arrival=0.0, phase_s=30.0) == []


def test_the_report_counts_requests_caught_by_a_flip(tmp_path):
    """§C: "in-flight requests at the moment of each flip"."""
    (tmp_path / "adaptive.0.3.journal.jsonl").write_text(
        json.dumps(
            {
                "arrived": 0.0,
                "output_len": 4,
                "wanted_len": 4,
                "ttft_s": 0.1,
                "tpot_s": 0.01,
                "error": None,
            }
        )
    )
    (tmp_path / "adaptive.0.3.state.after.json").write_text(
        json.dumps(
            {
                "flips": [
                    {
                        "at": 1.0,
                        "iid": "n1",
                        "to": "decode",
                        "by": "algorithm2",
                        "prefill_inflight": 4,
                        "decode_inflight": 1,
                    },
                    {
                        "at": 2.0,
                        "iid": "n2",
                        "to": "decode",
                        "by": "algorithm2",
                        "prefill_inflight": 0,
                        "decode_inflight": 0,
                    },
                ]
            }
        )
    )
    scored = read_run(tmp_path, 10.0, 0.125)
    assert scored[0].damaged == 5
    assert "damaged" in render(scored)


def test_the_oracle_column_is_absent_without_a_profile(tmp_path):
    """The §D gap needs the profiled curves to price a window."""
    (tmp_path / "adaptive.0.3.journal.jsonl").write_text(
        json.dumps(
            {
                "arrived": 0.0,
                "output_len": 4,
                "wanted_len": 4,
                "ttft_s": 0.1,
                "tpot_s": 0.01,
                "error": None,
            }
        )
    )
    scored = read_run(tmp_path, 10.0, 0.125, tmp_path / "missing.json")
    assert scored[0].wrong is None
    assert "-" in render(scored)


def test_the_oracle_column_reports_a_fraction_with_one(tmp_path):
    from narwhal.profiler import Profile, ProfileStore

    store = ProfileStore(tmp_path / "profiles.json")
    store.put(Profile("n1", 2e-8, 6e-5, 0.005, 3e-6, 0.012))

    rows = [
        {
            "arrived": float(k),
            "input_len": 400,
            "output_len": 8000,
            "tpot_s": 0.02,
            "wanted_len": 8000,
            "ttft_s": 0.1,
            "error": None,
        }
        for k in range(30)
    ]
    (tmp_path / "adaptive.0.3.journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (tmp_path / "adaptive.0.3.state.after.json").write_text(
        json.dumps(
            {"pools": {"prefill": ["n1", "n2", "n3"], "decode": ["n4", "n5", "n6"]}, "flips": []}
        )
    )

    scored = read_run(tmp_path, 10.0, 0.125, tmp_path / "profiles.json")
    assert scored[0].wrong == 1.0, "decode-heavy throughout, split never moved"
    assert "100%" in render(scored)


def test_a_named_cell_scores_without_a_rate(tmp_path):
    """A walk cell is `arm.walk.*`, not `arm.<rate>.*`; the report takes the
    tag as the cell name instead of refusing the directory."""
    rows = [
        {
            "arrived": 0.0,
            "output_len": 4,
            "wanted_len": 4,
            "ttft_s": 0.5,
            "tpot_s": 0.02,
            "error": None,
        },
    ]
    (tmp_path / "adaptive.walk.journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    scored = read_run(tmp_path, ttft_slo=3.0, tpot_slo=0.06)
    assert len(scored) == 1
    assert (scored[0].arm, scored[0].tag, scored[0].rate) == ("adaptive", "walk", None)
    assert "walk" in render(scored)


def test_the_handoff_block_reads_off_the_journal(tmp_path):
    """§E: first_byte - ttft, split by whether the KV crossed instances."""
    rows = [
        {
            "arrived": 0.0,
            "output_len": 4,
            "wanted_len": 4,
            "ttft_s": 1.0,
            "first_byte_s": 1.6,
            "crossed": True,
            "error": None,
            "tpot_s": 0.02,
        },
        {
            "arrived": 1.0,
            "output_len": 4,
            "wanted_len": 4,
            "ttft_s": 1.0,
            "first_byte_s": 3.0,
            "crossed": True,
            "error": None,
            "tpot_s": 0.02,
        },
        {
            "arrived": 2.0,
            "output_len": 4,
            "wanted_len": 4,
            "ttft_s": 1.0,
            "first_byte_s": 2.0,
            "crossed": False,
            "error": None,
            "tpot_s": 0.02,
        },
    ]
    (tmp_path / "adaptive.0.1.journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    scored = read_run(tmp_path, ttft_slo=10.0, tpot_slo=0.125)
    r = scored[0]
    assert r.handoff_crossed == [0.6000000000000001, 2.0]
    assert r.handoff_local == [1.0]
    assert r.crossed_share == 2 / 3
    out = render(scored)
    assert "KV handoff" in out
    assert "2.00" in out, "nearest-rank p50 of [0.6, 2.0] is the upper sample"
    assert "1.00" in out, "the same-instance column carries the local p50"


def test_time_to_adapt_honours_the_phase_count(tmp_path):
    """An eight-phase walk has seven boundaries, not two."""
    rows = [
        {
            "arrived": t,
            "output_len": 4,
            "wanted_len": 4,
            "ttft_s": 0.5,
            "tpot_s": 0.02,
            "error": None,
        }
        for t in (0.0, 800.0)
    ]
    (tmp_path / "adaptive.walk.journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (tmp_path / "adaptive.walk.state.after.json").write_text(
        json.dumps({"flips": [{"at": 105.0, "iid": "n1", "to": "decode", "by": "algorithm2"}]})
    )
    scored = read_run(tmp_path, ttft_slo=3.0, tpot_slo=0.06, phases=8)
    assert scored[0].adapt_s == [5.0], "one boundary at 100 s has a flip 5 s after"


def test_a_saturated_oracle_is_flagged_not_cited():
    """When every arm reads >=85% wrong, the column separates nothing
    and the renderer must say so."""
    from collections import Counter

    from narwhal.report import ArmRate, oracle_saturated, render

    def cell(arm, wrong):
        return ArmRate(
            arm=arm,
            tag="1.0",
            rate=1.0,
            met=90,
            total=100,
            reroles=0,
            reversals=0,
            damaged=0,
            wall_s=100.0,
            bound=Counter(met=90),
            adapt_s=[],
            wrong=wrong,
            handoff_crossed=[],
            handoff_local=[],
        )

    sat = [cell("a", 0.97), cell("b", 0.99)]
    assert oracle_saturated(sat)
    assert "saturated" in render(sat)
    ok = [cell("a", 0.38), cell("b", 0.72)]
    assert not oracle_saturated(ok)
    assert "saturated" not in render(ok)
