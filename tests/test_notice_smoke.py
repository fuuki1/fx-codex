"""Detailed notice end-to-end smoke tests."""

from __future__ import annotations

import csv
import json

from fx_intel import notice_journal, notice_quality, notice_smoke


def test_run_notice_pipeline_smoke_writes_all_artifacts(tmp_path) -> None:
    result = notice_smoke.run_notice_pipeline_smoke(tmp_path)

    assert result.outcome == notice_quality.OUTCOME_HIT
    assert result.entry_scenario == notice_quality.ENTRY_SCENARIO_PULLBACK
    assert result.chunk_count >= 1
    assert "T1先着1件" in result.summary_text
    assert result.report_markdown_path.exists()
    assert result.journal_path.exists()
    assert result.quality_json_path.exists()
    assert result.quality_csv_path.exists()
    assert result.feedback_path.exists()

    journal_rows = list(notice_journal.read_notice_entries(result.journal_path))
    assert len(journal_rows) == 1
    assert journal_rows[0]["delivery"] == "smoke"
    assert journal_rows[0]["entry_level_source"]["source"] == "recent_ohlc"

    quality = json.loads(result.quality_json_path.read_text(encoding="utf-8"))
    assert quality["summary"]["hits"] == 1
    assert quality["outcomes"][0]["outcome"] == notice_quality.OUTCOME_HIT
    assert quality["outcomes"][0]["entry_scenario"] == notice_quality.ENTRY_SCENARIO_PULLBACK

    rows = list(csv.DictReader(result.quality_csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0]["symbol"] == "USDJPY"
    assert rows[0]["outcome"] == notice_quality.OUTCOME_HIT

    feedback = json.loads(result.feedback_path.read_text(encoding="utf-8"))
    assert feedback["total"] == 1


def test_smoke_notice_pipeline_cli_reports_outputs(tmp_path, capsys) -> None:
    from fx_briefing import smoke_notice_pipeline_cli

    exit_code = smoke_notice_pipeline_cli(tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "詳細通知E2Eスモーク: OK" in output
    assert "notice_quality.json" in output
    assert (tmp_path / "notice_quality.csv").exists()
