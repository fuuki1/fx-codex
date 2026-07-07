"""Detailed notice operational health checks."""

from __future__ import annotations

from fx_intel import notice_health, notice_smoke


def test_notice_health_passes_for_smoke_artifacts(tmp_path) -> None:
    smoke = notice_smoke.run_notice_pipeline_smoke(tmp_path)

    report = notice_health.check_notice_health(
        journal_path=smoke.journal_path,
        feedback_path=smoke.feedback_path,
        webhook_url="https://discord.example/webhook",
        require_discord=True,
        quality_json_path=smoke.quality_json_path,
        quality_csv_path=smoke.quality_csv_path,
        smoke_dir=smoke.output_dir,
    )

    assert report.status == notice_health.STATUS_OK
    assert report.exit_code == 0
    assert "詳細通知ヘルスチェック: OK" in notice_health.format_health_report_ja(report)


def test_notice_health_warns_for_initial_missing_state(tmp_path) -> None:
    report = notice_health.check_notice_health(
        journal_path=tmp_path / "missing_journal.jsonl",
        feedback_path=tmp_path / "missing_feedback.json",
        smoke_dir=tmp_path / "missing_smoke",
    )

    assert report.status == notice_health.STATUS_WARN
    assert report.exit_code == 0
    assert {check.name for check in report.checks if check.status == notice_health.STATUS_WARN} == {
        "notice_journal",
        "notice_feedback",
        "notice_smoke",
    }


def test_notice_health_fails_for_required_discord_and_corrupt_feedback(tmp_path) -> None:
    smoke = notice_smoke.run_notice_pipeline_smoke(tmp_path / "smoke")
    bad_feedback = tmp_path / "bad_feedback.json"
    bad_feedback.write_text("{bad-json", encoding="utf-8")

    report = notice_health.check_notice_health(
        journal_path=smoke.journal_path,
        feedback_path=bad_feedback,
        require_discord=True,
        quality_json_path=smoke.quality_json_path,
        quality_csv_path=smoke.quality_csv_path,
        smoke_dir=smoke.output_dir,
    )

    assert report.status == notice_health.STATUS_FAIL
    assert report.exit_code == 1
    failed = {check.name for check in report.checks if check.status == notice_health.STATUS_FAIL}
    assert failed == {"discord_webhook", "notice_feedback"}


def test_notice_health_fails_for_malformed_journal_latest_row(tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    feedback_path = tmp_path / "feedback.json"
    journal_path.write_text(
        '{"ts":"2026-07-06T12:00:00+00:00","symbol":"USDJPY"}\n', encoding="utf-8"
    )
    feedback_path.write_text(
        '{"schema":3,"generated_at":"2026-07-06T12:00:00+00:00","cells":{"overall":{"key":"overall"}}}',
        encoding="utf-8",
    )

    report = notice_health.check_notice_health(
        journal_path=journal_path,
        feedback_path=feedback_path,
    )

    assert report.status == notice_health.STATUS_FAIL
    assert any(
        check.name == "notice_journal" and check.status == notice_health.STATUS_FAIL
        for check in report.checks
    )


def test_check_notice_health_cli_reports_status(tmp_path, capsys) -> None:
    from fx_briefing import check_notice_health_cli

    smoke = notice_smoke.run_notice_pipeline_smoke(tmp_path)

    exit_code = check_notice_health_cli(
        journal_path=smoke.journal_path,
        feedback_path=smoke.feedback_path,
        quality_json_path=smoke.quality_json_path,
        quality_csv_path=smoke.quality_csv_path,
        smoke_dir=smoke.output_dir,
        require_discord=False,
        max_journal_age_hours=None,
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "詳細通知ヘルスチェック: OK" in output
    assert "notice_journal" in output
