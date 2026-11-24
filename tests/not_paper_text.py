"""One assertion for the not-paper outcome text of fetch-bars, probe-data and the watcher.

Three lines exist: at open (nothing was requested), mid-run (the list failed after the account was
verified) and in doubt (closed, the account could not be confirmed). A run prints exactly one of
them, and its Result line carries the label that goes with it.
"""

from __future__ import annotations

from MainStoinker.ib import cli

LINES = {"open": cli.NOT_PAPER_LINE, "mid": cli.NOT_PAPER_MID_LINE, "doubt": cli.NOT_PAPER_DOUBT_LINE}
LABELS = {"open": cli.NOT_PAPER_LABEL, "mid": cli.NOT_PAPER_MID_LABEL, "doubt": cli.NOT_PAPER_DOUBT_LABEL}


def assert_one_not_paper_line(text: str, which: str) -> None:
    """Exactly one of the three lines is in `text`, it is `which` ("open", "mid" or "doubt"), printed
    once, and the Result line has the matching label."""
    counts = {name: text.count(line) for name, line in LINES.items()}
    assert counts == {name: int(name == which) for name in LINES}, counts
    results = [ln for ln in text.splitlines() if ln.startswith("Result: ")]
    assert len(results) == 1 and LABELS[which] in results[0], results
    for name, label in LABELS.items():
        if name != which:
            assert label not in results[0], (name, results[0])
