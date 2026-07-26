from pathlib import Path

from tools.capture_journal_crash_probe import run_probe


def test_process_crash_probe_passes_all_boundaries(tmp_path: Path) -> None:
    report = run_probe(tmp_path)

    assert report["passed"] is True
    assert all(stage["passed"] for stage in report["stages"].values())
