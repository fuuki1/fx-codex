"""The outcome-report memo must never serve bytes the file no longer has.

One briefing parses ``briefing_decision_outcomes.json`` from several call sites,
and on the Mac mini that file is 476 MB (9.3 s per parse). Memoizing it is only
safe because the key includes the file's mtime and size: ``save_outcome_report``
rewrites the report mid-run, and a path-keyed cache would then hand later
readers a superseded report.
"""

from __future__ import annotations

import json
import os

import fx_briefing
from fx_intel import journal
import pytest


def _report(marker: str) -> dict[str, object]:
    return {
        "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
        "pit_required": True,
        "marker": marker,
        "outcomes": [],
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    fx_briefing._OUTCOME_REPORT_CACHE.clear()  # noqa: SLF001
    yield
    fx_briefing._OUTCOME_REPORT_CACHE.clear()  # noqa: SLF001


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_repeated_loads_parse_the_file_once(tmp_path, monkeypatch) -> None:
    path = tmp_path / "briefing_decision_outcomes.json"
    _write(path, _report("first"))

    calls = {"n": 0}
    original = fx_briefing.decision_feedback.load_decision_outcome_report

    def counting(target):
        calls["n"] += 1
        return original(target)

    monkeypatch.setattr(fx_briefing.decision_feedback, "load_decision_outcome_report", counting)

    first = fx_briefing.load_pit_decision_outcome_report(path)
    second = fx_briefing.load_pit_decision_outcome_report(path)

    assert first == second
    assert first["marker"] == "first"
    assert calls["n"] == 1


def test_rewritten_report_is_re_read(tmp_path) -> None:
    """save_outcome_report rewrites the file mid-run; the memo must notice."""

    path = tmp_path / "briefing_decision_outcomes.json"
    _write(path, _report("before"))
    assert fx_briefing.load_pit_decision_outcome_report(path)["marker"] == "before"

    _write(path, _report("after"))
    # Same size is the hostile case: only the mtime distinguishes the revisions.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert fx_briefing.load_pit_decision_outcome_report(path)["marker"] == "after"


def test_reports_failing_the_pit_contract_stay_empty(tmp_path) -> None:
    path = tmp_path / "briefing_decision_outcomes.json"
    _write(path, {"pit_contract": "something-else", "pit_required": True})
    assert fx_briefing.load_pit_decision_outcome_report(path) == {}
    # The rejection is cached too, and must stay a rejection.
    assert fx_briefing.load_pit_decision_outcome_report(path) == {}


def test_report_without_pit_required_stays_empty(tmp_path) -> None:
    path = tmp_path / "briefing_decision_outcomes.json"
    _write(path, {"pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT, "pit_required": False})
    assert fx_briefing.load_pit_decision_outcome_report(path) == {}


def test_missing_file_is_not_cached(tmp_path) -> None:
    """An absent file must not poison the cache once it appears."""

    path = tmp_path / "briefing_decision_outcomes.json"
    assert fx_briefing.load_pit_decision_outcome_report(path) == {}

    _write(path, _report("arrived"))
    assert fx_briefing.load_pit_decision_outcome_report(path)["marker"] == "arrived"
