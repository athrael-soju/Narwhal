"""The meta row: written once per writer, invisible to every reader."""

from __future__ import annotations

import json
from pathlib import Path

from narwhal.bench import main as bench_main
from narwhal.bench import score_journal
from narwhal.journal import RunJournal
from narwhal.provenance import stamp
from narwhal.report import read_run


def test_the_stamp_names_the_package():
    meta = stamp()["meta"]
    assert meta["package"] == "narwhal-inference"
    assert meta["version"]


def test_a_journal_opens_with_a_meta_row(tmp_path: Path):
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    journal.open()
    journal.write({"rid": "r1"})
    journal.close()
    rows = [json.loads(x) for x in journal.path.read_text().splitlines()]
    assert "meta" in rows[0]
    assert rows[1] == {"run": journal.run, "rid": "r1"}, "every request row carries the run id"


def test_each_reopen_stamps_again(tmp_path: Path):
    """An appended-to journal records every process that wrote it."""
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    for _ in range(2):
        journal.open()
        journal.close()
    rows = [json.loads(x) for x in journal.path.read_text().splitlines()]
    assert [list(r) for r in rows] == [["meta"], ["meta"]]


def _request_row(rid: str, ttft: float) -> dict:
    return {
        "rid": rid,
        "input_len": 100,
        "output_len": 10,
        "wanted_len": 10,
        "ttft_s": ttft,
        "tpot_s": 0.05,
        "first_byte_s": ttft + 0.2,
        "crossed": True,
        "arrived": 0.0,
        "error": None,
    }


def test_score_journal_skips_the_meta_row(tmp_path: Path):
    path = tmp_path / "journal.jsonl"
    with path.open("w") as fh:
        fh.write(json.dumps(stamp()) + "\n")
        fh.write(json.dumps(_request_row("r1", ttft=0.5)) + "\n")
        fh.write(json.dumps(_request_row("r2", ttft=9.0)) + "\n")
    _frac, met, total = score_journal(path, ttft_slo=1.0, tpot_slo=0.125)
    assert (met, total) == (1, 2), "the meta row must not enter the denominator"


def test_read_run_skips_the_meta_row(tmp_path: Path):
    journal = tmp_path / "arm.walk.journal.jsonl"
    with journal.open("w") as fh:
        fh.write(json.dumps(stamp()) + "\n")
        fh.write(json.dumps(_request_row("r1", ttft=0.5)) + "\n")
    rows = read_run(tmp_path, ttft_slo=1.0, tpot_slo=0.125)
    assert len(rows) == 1
    assert rows[0].total == 1


def test_the_bench_cli_scores_a_journal_without_driving(tmp_path: Path, capsys):
    path = tmp_path / "journal.jsonl"
    with path.open("w") as fh:
        fh.write(json.dumps(stamp()) + "\n")
        fh.write(json.dumps(_request_row("r1", ttft=0.5)) + "\n")
    rc = bench_main(["--score-journal", str(path), "--ttft-slo", "1.0", "--tpot-slo", "0.125"])
    assert rc == 0
    assert "1/1" in capsys.readouterr().out


def test_the_deploy_stamp_is_the_fallback_describe(tmp_path):
    """A tarball deploy has no .git, so the journal's build identity
    comes from the DEPLOYED_COMMIT file the deploy dropped next to the code."""
    from narwhal.provenance import _read_stamp

    stamp = tmp_path / "DEPLOYED_COMMIT"
    stamp.write_text("v0.1.0-99-gabc1234\n")
    assert _read_stamp(stamp) == "v0.1.0-99-gabc1234"
    assert _read_stamp(tmp_path / "absent") is None
    (tmp_path / "empty").write_text("  \n")
    assert _read_stamp(tmp_path / "empty") is None


def test_the_payload_carries_the_source_commit(monkeypatch):
    import base64
    import io
    import tarfile

    from narwhal import fleet

    monkeypatch.setattr(fleet, "_source_commit", lambda: "gdeadbee-dirty")
    raw = base64.b64decode(fleet._payload(("pyproject.toml",)))
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        names = tar.getnames()
        assert "src/narwhal/DEPLOYED_COMMIT" in names
        member = tar.extractfile("src/narwhal/DEPLOYED_COMMIT")
        assert member is not None
        assert member.read().decode().strip() == "gdeadbee-dirty"
