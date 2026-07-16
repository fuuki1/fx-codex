"""tools/learning_loop_audit.py の監査ロジック検証(合成ログ・ネットワーク不要)。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

from tools import learning_loop_audit as audit

# 2026-07-13(月) 12:00 UTC を基準にすると、直近72hに前週末クローズが1回入る
MONDAY_NOON = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
TUESDAY_NOON = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _price_row(ts: datetime, symbol: str = "USDJPY", timeframe: str = "1h", close: float = 150.0):
    return {"ts": ts.isoformat(), "symbol": symbol, "timeframe": timeframe, "close": close}


def _judgment_row(
    ts: datetime,
    symbol: str = "USDJPY",
    timeframe: str = "1h",
    direction: str = "long",
    close: float = 150.0,
    atr: float = 0.2,
    **extra,
):
    row = {
        "ts": ts.isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "close": close,
        "atr": atr,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 市場時間の複製がfx_intel.marketと一致するか


def test_market_open_boundaries_match_weekend_closure() -> None:
    friday_2059 = datetime(2026, 7, 10, 20, 59, tzinfo=UTC)
    friday_2100 = datetime(2026, 7, 10, 21, 0, tzinfo=UTC)
    sunday_2159 = datetime(2026, 7, 12, 21, 59, tzinfo=UTC)
    sunday_2200 = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)
    assert audit.is_market_open(friday_2059)
    assert not audit.is_market_open(friday_2100)
    assert not audit.is_market_open(sunday_2159)
    assert audit.is_market_open(sunday_2200)


def test_open_hours_between_excludes_weekend() -> None:
    friday_noon = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    # 金曜12:00→月曜12:00 = 壁時計72h - 週末49h = 23h
    assert audit.open_hours_between(friday_noon, MONDAY_NOON) == pytest.approx(23.0)
    # 週末を跨がない平日24hはそのまま
    assert audit.open_hours_between(MONDAY_NOON, MONDAY_NOON + timedelta(hours=24)) == (
        pytest.approx(24.0)
    )


def test_open_hours_replica_matches_fx_intel_market() -> None:
    """複製実装が本体(fx_intel.market)と乖離したらここで検出する。"""
    market = pytest.importorskip("fx_intel.market")
    checkpoints = [
        datetime(2026, 7, 9, 3, 0, tzinfo=UTC),
        datetime(2026, 7, 10, 22, 30, tzinfo=UTC),
        datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
        datetime(2026, 7, 12, 23, 0, tzinfo=UTC),
        MONDAY_NOON,
    ]
    for moment in checkpoints:
        assert audit.is_market_open(moment) == market.is_market_open(moment), moment
    start = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    for end in checkpoints:
        if end <= start:
            continue
        assert audit.open_hours_between(start, end) == pytest.approx(
            market.open_hours_between(start, end), abs=1e-6
        ), end


# ---------------------------------------------------------------------------
# 採点(成熟・対応付け・hit/miss/flat)


def test_score_predictions_hit_miss_flat_and_immature(tmp_path) -> None:
    base = MONDAY_NOON
    now = base + timedelta(hours=3)
    journal = audit.JsonlFile(path=tmp_path / "j.jsonl", exists=True)
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    # 1h足: 主ホライズン1h±0.25h
    journal.rows = [
        _judgment_row(base, direction="long", close=150.0, atr=0.2),  # +0.5 → hit
        _judgment_row(base, symbol="EURUSD", direction="short", close=1.1, atr=0.01),  # 上昇 → miss
        _judgment_row(base, symbol="GBPUSD", direction="long", close=1.3, atr=0.5),  # 微動 → flat
        _judgment_row(now - timedelta(minutes=10), direction="long"),  # 未成熟
    ]
    prices.rows = [
        _price_row(base + timedelta(hours=1), close=150.5),
        _price_row(base + timedelta(hours=1), symbol="EURUSD", close=1.2),
        _price_row(base + timedelta(hours=1), symbol="GBPUSD", close=1.301),
    ]
    result = audit.score_timeframe_predictions(journal, prices, now)
    assert result.matured_scored == 3
    assert result.outcomes == {"hit": 1, "miss": 1, "flat": 1}
    assert result.immature == 1
    assert result.matured_unresolved == 0


def test_score_predictions_reports_unresolved_when_price_missing(tmp_path) -> None:
    journal = audit.JsonlFile(path=tmp_path / "j.jsonl", exists=True)
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    journal.rows = [_judgment_row(MONDAY_NOON)]
    result = audit.score_timeframe_predictions(journal, prices, MONDAY_NOON + timedelta(hours=6))
    assert result.matured_unresolved == 1
    assert result.unresolved_reasons == {"no_future_price_in_tolerance": 1}


def test_score_predictions_matures_on_open_hours_not_wall_clock(tmp_path) -> None:
    """金曜21時直前の1d判断は、週末49hを除いた24hで成熟する。"""
    friday_20 = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    journal = audit.JsonlFile(path=tmp_path / "j.jsonl", exists=True)
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    journal.rows = [_judgment_row(friday_20, timeframe="1d")]
    # 壁時計では月曜20時=+72hだが、オープン時間では 1h+22h=23h → 未成熟
    monday_20 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
    result = audit.score_timeframe_predictions(journal, prices, monday_20)
    assert result.immature == 1
    # 火曜23時 → オープン26h(>24+2) で成熟(価格が無いのでunresolved)
    tuesday_23 = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
    result = audit.score_timeframe_predictions(journal, prices, tuesday_23)
    assert result.matured_unresolved == 1


# ---------------------------------------------------------------------------
# セクション判定


def _steady_prices(start: datetime, hours: float, symbols=("USDJPY",), timeframes=("1h",)):
    rows = []
    moment = start
    end = start + timedelta(hours=hours)
    while moment <= end:
        if audit.is_market_open(moment):
            for symbol in symbols:
                for timeframe in timeframes:
                    rows.append(_price_row(moment, symbol=symbol, timeframe=timeframe))
        moment += timedelta(minutes=5)
    return rows


def test_data_collection_pass_on_steady_capture(tmp_path) -> None:
    now = TUESDAY_NOON
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    prices.rows = _steady_prices(now - timedelta(hours=6), 6)
    section = audit.audit_data_collection(prices, now, 6.0, {"USDJPY"})
    assert section["status"] == audit.PASS
    cell = section["evidence"]["cells"]["USDJPY:1h"]
    assert cell["window_coverage_ratio"] > 0.95
    assert cell["effective_coverage_ratio"] > 0.95
    assert cell["max_open_market_gap_minutes"] <= 5.1


def test_data_collection_newly_started_cell_uses_effective_coverage(tmp_path) -> None:
    """窓の途中で収集が始まった新セルを全窓カバレッジで failにしない。"""
    now = TUESDAY_NOON
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    # 72h窓のうち直近2hだけ収集(新symbol追加直後を模す)
    prices.rows = _steady_prices(now - timedelta(hours=2), 2)
    section = audit.audit_data_collection(prices, now, 72.0, {"USDJPY"})
    assert section["status"] == audit.PASS, section["summary_ja"]
    cell = section["evidence"]["cells"]["USDJPY:1h"]
    assert cell["window_coverage_ratio"] < 0.1
    assert cell["effective_coverage_ratio"] > 0.95


def test_data_collection_fails_when_capture_stopped(tmp_path) -> None:
    now = TUESDAY_NOON
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    prices.rows = _steady_prices(now - timedelta(hours=6), 4)  # 最後の2hが停止
    section = audit.audit_data_collection(prices, now, 6.0, {"USDJPY"})
    assert section["status"] == audit.FAIL
    assert "最終価格" in section["summary_ja"]


def test_data_collection_ignores_weekend_gap(tmp_path) -> None:
    friday_20 = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    now = datetime(2026, 7, 12, 23, 0, tzinfo=UTC)  # 日曜23時(再開1h後)
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    prices.rows = _steady_prices(friday_20, 51.0)  # 週末を挟む(休場中は行なし)
    section = audit.audit_data_collection(prices, now, 51.0, {"USDJPY"})
    assert section["status"] == audit.PASS, section["summary_ja"]


def test_duplicate_detection_flags_duplicate_rows(tmp_path) -> None:
    now = TUESDAY_NOON
    journal = audit.JsonlFile(path=tmp_path / "j.jsonl", exists=True)
    ts = now - timedelta(minutes=30)
    journal.rows = [_judgment_row(ts) for _ in range(3)]
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    section = audit.audit_duplicates(journal, prices, {}, now, 6.0)
    assert section["status"] == audit.FAIL
    assert section["evidence"]["journal_duplicate_rows"] == 2


def test_duplicate_detection_reports_err_log_hits(tmp_path) -> None:
    now = TUESDAY_NOON
    journal = audit.JsonlFile(path=tmp_path / "j.jsonl", exists=True)
    journal.rows = [_judgment_row(now - timedelta(minutes=5))]
    prices = audit.JsonlFile(path=tmp_path / "p.jsonl", exists=True)
    section = audit.audit_duplicates(journal, prices, {audit.DUPLICATE_WRITER_PATTERN: 1}, now, 6.0)
    assert section["status"] == audit.WARN
    assert "duplicate writer" in section["summary_ja"]


def test_learning_update_parses_flat_profile_keys(tmp_path) -> None:
    now = TUESDAY_NOON
    tf_learning = {
        "generated_at": now.isoformat(),
        "profiles": {
            "USDJPY|1h": {
                "evaluated": 25,
                "tech_weight": 0.62,
                "news_weight": 0.38,
                "symbol_factors": {"USDJPY": 0.85},
                "condition_factors": {"rsi_1h": {"中立圏(35-65)": {"long": 0.9}}},
            },
            "EURUSD|15m": {"evaluated": 0, "tech_weight": 0.55},
        },
    }
    section = audit.audit_learning_update(tf_learning, now - timedelta(minutes=5), None, now)
    assert section["status"] == audit.PASS
    evidence = section["evidence"]
    assert evidence["cells_with_scored_samples"] == 1
    assert evidence["cells_with_adjusted_weights"] == 1
    assert evidence["cells_with_conviction_damping"] == 1
    assert evidence["cells_with_condition_factors"] == 1
    assert evidence["cell_scored_samples"]["USDJPY|1h"] == 25


def test_learning_update_warns_when_stale_or_empty(tmp_path) -> None:
    now = TUESDAY_NOON
    section = audit.audit_learning_update({"profiles": {}}, now - timedelta(hours=12), None, now)
    assert section["status"] == audit.WARN
    section = audit.audit_learning_update(None, None, None, now)
    assert section["status"] == audit.FAIL


def test_decision_application_detects_nondefault_weight(tmp_path) -> None:
    now = TUESDAY_NOON
    journal = audit.JsonlFile(path=tmp_path / "j.jsonl", exists=True)
    journal.rows = [
        _judgment_row(
            now - timedelta(minutes=10),
            components=[{"key": "tech", "weight": 0.62}, {"key": "news", "weight": 0.38}],
            net_expected_r=0.12,
        )
    ]
    learning_section = {"evidence": {"cells_with_adjusted_weights": 1}}
    section = audit.audit_decision_application(journal, learning_section, now, 6.0)
    assert section["status"] == audit.PASS
    assert section["evidence"]["rows_with_nondefault_tech_weight"] == 1
    assert section["evidence"]["rows_with_net_expected_r"] == 1


def test_decision_application_warns_when_learned_but_not_applied(tmp_path) -> None:
    now = TUESDAY_NOON
    journal = audit.JsonlFile(path=tmp_path / "j.jsonl", exists=True)
    journal.rows = [
        _judgment_row(
            now - timedelta(minutes=10),
            components=[{"key": "tech", "weight": audit.DEFAULT_TECH_WEIGHT}],
        )
    ]
    learning_section = {"evidence": {"cells_with_adjusted_weights": 2}}
    section = audit.audit_decision_application(journal, learning_section, now, 6.0)
    assert section["status"] == audit.WARN
    assert "非既定重みが1件も現れない" in section["summary_ja"]


def test_scanner_429_thresholds() -> None:
    clean = audit.audit_scanner_429({}, {"scanner_errors": 0, "timeouts": 0}, {}, True)
    assert clean["status"] == audit.PASS
    few = audit.audit_scanner_429({}, {"scanner_errors": 2, "timeouts": 0}, {}, True)
    assert few["status"] == audit.WARN
    # 痕跡が多くても現在の収集が健全なら残存扱いのwarn
    stale_traces = audit.audit_scanner_429({}, {"scanner_errors": 151, "timeouts": 0}, {}, True)
    assert stale_traces["status"] == audit.WARN
    assert "残存" in stale_traces["summary_ja"]
    # 収集も不健全なら再発中としてfail
    recurring = audit.audit_scanner_429({}, {"scanner_errors": 151, "timeouts": 0}, {}, False)
    assert recurring["status"] == audit.FAIL


def test_freshness_mirror() -> None:
    now = TUESDAY_NOON
    report = {
        "monitor_timestamp": (now - timedelta(minutes=3)).isoformat(),
        "targets": [
            {"name": "tf_price_snapshot", "status": "ok"},
            {"name": "tf_journal", "status": "warning"},
        ],
    }
    section = audit.audit_freshness(report, now)
    assert section["status"] == audit.WARN
    assert section["evidence"]["target_statuses"]["tf_journal"] == "warning"
    assert audit.audit_freshness(None, now)["status"] == audit.FAIL


def test_aggregate_overall_fail_dominates() -> None:
    sections = {
        "a": {"status": audit.PASS},
        "b": {"status": audit.WARN},
        "c": {"status": audit.FAIL},
    }
    assert audit.aggregate_overall(sections) == audit.FAIL
    sections["c"]["status"] = audit.PASS
    assert audit.aggregate_overall(sections) == audit.WARN


# ---------------------------------------------------------------------------
# E2E: run_audit + CLI


def _build_log_dir(tmp_path: Path, now: datetime) -> Path:
    log_dir = tmp_path / "logs"
    base = now - timedelta(hours=3)
    price_rows = _steady_prices(now - timedelta(hours=6), 6)
    judgment_rows = []
    moment = now - timedelta(hours=6)
    while moment <= now:
        if audit.is_market_open(moment):
            judgment_rows.append(
                _judgment_row(
                    moment,
                    components=[{"key": "tech", "weight": 0.55}],
                )
            )
        moment += timedelta(minutes=5)
    _write_jsonl(log_dir / "briefing_tf_prices.jsonl", price_rows)
    _write_jsonl(log_dir / "briefing_tf_journal.jsonl", judgment_rows)
    _write_jsonl(
        log_dir / "briefing_journal.jsonl",
        [
            {
                "ts": base.isoformat(),
                "symbol": "USDJPY",
                "direction": "long",
                "close": 150.0,
                "pit_eligible": True,
            }
        ],
    )
    (log_dir / "briefing_tf_learning.json").write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "profiles": {"USDJPY|1h": {"evaluated": 10, "tech_weight": 0.55}},
            }
        ),
        encoding="utf-8",
    )
    (log_dir / "briefing_learning.json").write_text(
        json.dumps({"generated_at": now.isoformat(), "evaluated": 0}), encoding="utf-8"
    )
    (log_dir / "briefing_decision_outcomes.json").write_text(
        json.dumps(
            {
                "summary": {
                    "overall": {
                        "evaluated": 5,
                        "tradable": 2,
                        "avg_mfe_r": 0.4,
                        "avg_mae_r": -0.5,
                        "tp1_rate": 0.5,
                        "sl_rate": 0.5,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (log_dir / "briefing_decision_feedback.json").write_text(
        json.dumps({"generated_at": now.isoformat(), "cells": {}}), encoding="utf-8"
    )
    (log_dir / "freshness_report.json").write_text(
        json.dumps(
            {
                "monitor_timestamp": (now - timedelta(minutes=2)).isoformat(),
                "targets": [{"name": "tf_price_snapshot", "status": "ok"}],
            }
        ),
        encoding="utf-8",
    )
    (log_dir / "launchd").mkdir(parents=True, exist_ok=True)
    (log_dir / "launchd" / "snapshot.err.log").write_text("", encoding="utf-8")
    return log_dir


def test_run_audit_end_to_end_and_cli_outputs(tmp_path, capsys) -> None:
    now = TUESDAY_NOON
    log_dir = _build_log_dir(tmp_path, now)
    json_out = tmp_path / "reports" / "audit.json"
    md_out = tmp_path / "reports" / "audit.md"
    rc = audit.main(
        [
            "--log-dir",
            str(log_dir),
            "--window-hours",
            "6",
            "--now",
            now.isoformat(),
            "--no-launchd",
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(md_out),
        ]
    )
    out = capsys.readouterr().out
    assert "overall_status:" in out
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["overall_status"] in ("pass", "warn")
    assert rc == {"pass": 0, "warn": 1}.get(report["overall_status"])
    for key in (
        "data_collection",
        "prediction_capture",
        "outcome_maturation",
        "trade_outcome",
        "learning_update",
        "decision_application",
        "duplicate_detection",
        "freshness",
        "scanner_429",
        "sample_sufficiency",
        "blocking_reasons",
    ):
        assert key in report["sections"], key
    markdown = md_out.read_text(encoding="utf-8")
    assert "E2E学習ループ監査レポート" in markdown
    assert "| data_collection |" in markdown


def test_cli_rejects_invalid_inputs(tmp_path) -> None:
    assert audit.main(["--window-hours", "0"]) == 3
    assert audit.main(["--now", "2026-07-14T12:00:00"]) == 3  # timezone欠落


def test_run_audit_fails_when_everything_missing(tmp_path) -> None:
    report = audit.run_audit(tmp_path / "empty", 72.0, now=TUESDAY_NOON)
    assert report["overall_status"] == "fail"
    assert report["sections"]["data_collection"]["status"] == audit.FAIL
