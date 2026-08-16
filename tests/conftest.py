"""Fixtures shared by the suite.

The tests build routers directly rather than through `create_app`, so nothing
runs the shutdown handler that closes the journal. The handle then reaches the
garbage collector open, and CPython raises `ResourceWarning` from whatever
callback happened to trigger the collection. `filterwarnings` in
`pyproject.toml` promotes that warning to an error.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from narwhal.journal import RunJournal


@pytest.fixture(autouse=True)
def close_journals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every journal the test opened, in the order it opened them.

    `RunJournal.close` is idempotent, so a test that closes its own journal is
    unaffected.
    """
    opened: list[RunJournal] = []
    open_one = RunJournal.open

    def tracked_open(journal: RunJournal) -> None:
        open_one(journal)
        opened.append(journal)

    monkeypatch.setattr(RunJournal, "open", tracked_open)
    yield
    for journal in opened:
        journal.close()
