from pathlib import Path

from tools.capture_journal_archive_probe import run_probe


def test_archive_restore_probe_passes(tmp_path: Path) -> None:
    report = run_probe(tmp_path)

    assert report["passed"] is True
    assert report["source_shards_preserved"] is True
    assert report["restored_quotes_match_source_prefix"] is True
