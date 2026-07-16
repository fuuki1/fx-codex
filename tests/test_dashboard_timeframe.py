"""AI learning dashboard の時間足別採点(_evaluate_journal)のテスト。

dashboard は fx_intel 非依存の独立ツールなので、パス経由で import する。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

_SERVER_PATH = Path(__file__).resolve().parents[1] / "tools" / "ai_learning_dashboard" / "server.py"


@pytest.fixture(scope="module")
def server():
    spec = importlib.util.spec_from_file_location("dashboard_server", _SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


START = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)  # 月曜(オープン中)


def _row(ts, timeframe, horizon, direction, close, atr=0.10):
    return {
        "ts": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": timeframe,
        "horizon_hours": horizon,
        "direction": direction,
        "conviction": 50,
        "close": close,
        "atr": atr,
    }


def test_scores_each_timeframe_at_its_horizon(server) -> None:
    entries = [
        # 1h long: 1時間後に上昇 → hit
        _row(START, "1h", 1.0, "long", 156.0),
        _row(START + timedelta(hours=1), "1h", 1.0, "long", 156.4),
        # 15m long: 15分後に下落 → miss
        _row(START, "15m", 0.25, "long", 150.0, atr=0.05),
        _row(START + timedelta(minutes=15), "15m", 0.25, "long", 149.8, atr=0.05),
    ]
    result = server._evaluate_journal(entries)
    assert result["evaluated"] == 2
    assert result["hits"] == 1
    by_tf = result["by_timeframe"]
    assert by_tf["1h"] == {"evaluated": 1, "hits": 1, "flat": 0}
    assert by_tf["15m"] == {"evaluated": 1, "hits": 0, "flat": 0}


def test_legacy_rows_without_timeframe_use_24h(server) -> None:
    # timeframe 無し(融合1判断)は 24h ホライズンで採点、by_timeframe には出ない
    entries = [
        {
            "ts": START.isoformat(),
            "symbol": "USDJPY",
            "direction": "long",
            "close": 156.0,
            "atr": 0.10,
        },
        {
            "ts": (START + timedelta(hours=24)).isoformat(),
            "symbol": "USDJPY",
            "direction": "long",
            "close": 157.0,
            "atr": 0.10,
        },
    ]
    result = server._evaluate_journal(entries)
    assert result["evaluated"] == 1
    assert result["hits"] == 1
    assert result["by_timeframe"] == {}  # 旧行は時間足別内訳に入らない


def test_series_separated_by_timeframe(server) -> None:
    """同じ ts でも 15m と 1h は別系列で採点される(混ざらない)。"""
    entries = [
        _row(START, "15m", 0.25, "long", 150.0, atr=0.05),
        _row(START, "1h", 1.0, "long", 150.0),
        # 15m の15分後(上昇=hit)
        _row(START + timedelta(minutes=15), "15m", 0.25, "long", 150.5, atr=0.05),
        # 1h の1時間後(下落=miss)。15m系列と混ざらないこと
        _row(START + timedelta(hours=1), "1h", 1.0, "long", 149.5),
    ]
    result = server._evaluate_journal(entries)
    assert result["by_timeframe"]["15m"]["hits"] == 1
    assert result["by_timeframe"]["1h"]["hits"] == 0


def test_recent_outcomes_include_timeframe(server) -> None:
    entries = [
        _row(START, "4h", 4.0, "long", 156.0, atr=0.3),
        _row(START + timedelta(hours=4), "4h", 4.0, "long", 157.0, atr=0.3),
    ]
    result = server._evaluate_journal(entries)
    assert result["recent_outcomes"][0]["timeframe"] == "4h"


def test_tolerance_scales_with_horizon(server) -> None:
    assert server._tolerance_for(0.25) < server._tolerance_for(24.0)
    assert server._tolerance_for(999.0) == 2.0


def test_build_state_includes_trade_outcome_monitor(server, tmp_path) -> None:
    registry = {
        "generated_at": START.isoformat(),
        "candidates": {
            "cand-ready": {
                "candidate_id": "cand-ready",
                "status": "active",
                "stage": "paper_ready",
                "priority": "high",
                "title_ja": "承認待ち候補",
                "seen_count": 2,
            },
            "cand-paused": {
                "candidate_id": "cand-paused",
                "status": "active",
                "stage": "auto_paused",
                "priority": "medium",
                "title_ja": "停止候補",
                "seen_count": 4,
                "auto_pause_reason_ja": "期待Rが悪化",
            },
        },
        "events": [
            {
                "ts": START.isoformat(),
                "candidate_id": "cand-paused",
                "event_type": "auto_paused",
                "from_stage": "approved",
                "to_stage": "auto_paused",
            }
        ],
    }
    monitor = {
        "generated_at": (START + timedelta(hours=1)).isoformat(),
        "status": "fail",
        "exit_code": 1,
        "registry": {
            "active_count": 2,
            "paper_ready_count": 1,
            "approved_count": 0,
            "auto_paused_count": 1,
            "rejected_count": 0,
            "resolved_count": 0,
        },
        "approved_policy_stats": [
            {
                "candidate_id": "cand-paused",
                "stage": "auto_paused",
                "tradable": 12,
                "expectancy_r": -0.2,
                "profit_factor_r": 0.8,
            }
        ],
        "alerts": [{"type": "auto_paused", "severity": "warn", "candidate_id": "cand-paused"}],
    }
    (tmp_path / "trade_improvement_candidates.json").write_text(
        json.dumps(registry),
        encoding="utf-8",
    )
    (tmp_path / "trade_outcome_monitor.json").write_text(
        json.dumps(monitor),
        encoding="utf-8",
    )

    state = server.build_state(tmp_path)
    trade = state["trade_monitor"]

    assert trade["status"] == "fail"
    assert trade["counts"]["paper_ready"] == 1
    assert trade["counts"]["auto_paused"] == 1
    assert trade["paper_ready"][0]["candidate_id"] == "cand-ready"
    assert trade["approved_policy_stats"][0]["candidate_id"] == "cand-paused"
    assert trade["recent_events"][0]["event_type"] == "auto_paused"


def test_build_state_includes_decision_expectancy_monitor(server, tmp_path) -> None:
    monitor = {
        "generated_at": START.isoformat(),
        "status": "fail",
        "exit_code": 1,
        "summary": {
            "decision_events": 25,
            "scored_outcomes": 25,
            "overall": {"expectancy_r": -1.0, "profit_factor_r": 0.0, "tradable": 25},
            "action_counts": {"avoid": 1},
            "failure_reason_summary": [
                {"key": "sl_first", "label_ja": "SL先着", "count": 25, "primary_count": 25}
            ],
            "worst_cells": [
                {
                    "symbol": "USDJPY",
                    "timeframe": "1h",
                    "direction": "long",
                    "tradable": 25,
                    "expectancy_r": -1.0,
                    "action": "avoid",
                }
            ],
        },
        "profile": {
            "cells": {
                "USDJPY|1h|long": {
                    "symbol": "USDJPY",
                    "timeframe": "1h",
                    "direction": "long",
                    "tradable": 25,
                    "expectancy_r": -1.0,
                    "sl_rate": 1.0,
                    "factor": 0.45,
                    "action": "avoid",
                }
            }
        },
    }
    (tmp_path / "decision_expectancy_monitor.json").write_text(
        json.dumps(monitor),
        encoding="utf-8",
    )

    state = server.build_state(tmp_path)
    decision = state["decision_monitor"]

    assert decision["status"] == "fail"
    assert decision["overall"]["expectancy_r"] == -1.0
    assert decision["scored_outcomes"] == 25
    assert decision["actionable_cells"][0]["action"] == "avoid"
    assert decision["failure_reason_summary"][0]["key"] == "sl_first"


def test_build_state_uses_timeframe_learning_when_fusion_learning_missing(
    server,
    tmp_path,
) -> None:
    tf_learning = {
        "generated_at": START.isoformat(),
        "per_timeframe": {
            "15m": {
                "generated_at": START.isoformat(),
                "evaluated": 29,
                "hits": 16,
                "flat": 6,
                "tech_weight": 0.63,
                "news_weight": 0.37,
                "tech_hit_rate": 0.55,
                "news_hit_rate": 0.52,
                "conviction_brier": 0.33,
                "conviction_brier_base": 0.247,
            },
            "1h": {
                "generated_at": START.isoformat(),
                "evaluated": 34,
                "hits": 17,
                "flat": 6,
                "tech_weight": 0.35,
                "news_weight": 0.65,
                "tech_hit_rate": 0.50,
                "news_hit_rate": 0.62,
            },
        },
    }
    (tmp_path / "briefing_tf_learning.json").write_text(
        json.dumps(tf_learning),
        encoding="utf-8",
    )

    state = server.build_state(tmp_path)

    assert state["learning_source"]["mode"] == "timeframe"
    assert state["learning"]["source"] == "timeframe"
    assert state["learning"]["evaluated"] == 63
    assert state["learning"]["hits"] == 33
    assert state["learning"]["tech_weight"] == pytest.approx(((0.63 * 29) + (0.35 * 34)) / 63)
    assert state["tf_learning"]["timeframes"][0]["timeframe"] == "15m"
    assert state["tf_learning"]["timeframes"][0]["hit_rate"] == pytest.approx(16 / 29)


def test_timeframe_summary_exposes_symbols_and_conditions(server, tmp_path) -> None:
    tf_learning = {
        "generated_at": START.isoformat(),
        "per_timeframe": {
            "15m": {
                "generated_at": START.isoformat(),
                "evaluated": 6,
                "hits": 3,
                "flat": 0,
                "tech_weight": 0.55,
                "news_weight": 0.45,
                "symbol_stats": {
                    "USDJPY": {"evaluated": 3, "hits": 1},
                    "EURUSD": {"evaluated": 3, "hits": 2},
                },
                "symbol_factors": {"USDJPY": 0.9},
                "condition_stats": {
                    "rsi_1h": {"中立圏(35-65)": {"long": {"evaluated": 3, "hits": 1}}},
                },
                "condition_factors": {},
                "notes_ja": ["過去の方向判断6件を15分後の値動きで採点 — 的中率 50%"],
            }
        },
    }
    (tmp_path / "briefing_tf_learning.json").write_text(
        json.dumps(tf_learning, ensure_ascii=False),
        encoding="utf-8",
    )

    state = server.build_state(tmp_path)
    row = state["tf_learning"]["timeframes"][0]

    assert row["timeframe"] == "15m"
    symbols = {s["symbol"]: s for s in row["symbols"]}
    assert symbols["EURUSD"]["hit_rate"] == pytest.approx(2 / 3)
    assert symbols["USDJPY"]["factor"] == pytest.approx(0.9)
    assert any(
        c["feature"] == "rsi_1h" and c["bucket"] == "中立圏(35-65)" for c in row["conditions"]
    )
    assert row["notes_ja"]


def test_journal_activity_buckets_by_direction(server, tmp_path) -> None:
    rows = [
        _row(START, "15m", 0.25, "long", 150.0),
        _row(START + timedelta(minutes=20), "15m", 0.25, "short", 150.0),
        _row(START + timedelta(hours=2), "1h", 1.0, "neutral", 150.0),
    ]
    (tmp_path / "briefing_tf_journal.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )

    state = server.build_state(tmp_path)
    activity = state["journal"]["activity"]

    assert activity["by_direction"]["long"] == 1
    assert activity["by_direction"]["short"] == 1
    assert activity["by_direction"]["neutral"] == 1
    # 同じ1時間バケットに long+short の2件が入る
    filled = [b for b in activity["buckets"] if b["total"] > 0]
    assert any(b["long"] == 1 and b["short"] == 1 for b in filled)
    assert sum(b["total"] for b in activity["buckets"]) == 3


def test_build_state_reports_missing_learning_ops_inputs(server, tmp_path) -> None:
    state = server.build_state(tmp_path, now=START, ps_output="")
    ops = state["ops"]

    assert ops["status"] == "warn"
    assert ops["signals"]["has_any_journal"] is False
    assert ops["signals"]["has_timeframe_prices"] is False
    assert ops["signals"]["has_any_learning"] is False
    assert ops["processes"][0]["running"] is False
    assert ops["processes"][1]["running"] is False
    assert any("判断ログ" in alert["message_ja"] for alert in ops["alerts"])
    assert any("スナップショットループ" in alert["message_ja"] for alert in ops["alerts"])


def test_build_state_reports_running_learning_ops(server, tmp_path) -> None:
    (tmp_path / "briefing_tf_journal.jsonl").write_text(
        json.dumps(_row(START, "15m", 0.25, "long", 150.0), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "briefing_tf_prices.jsonl").write_text(
        json.dumps(_row(START + timedelta(minutes=15), "15m", 0.25, "neutral", 150.2)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "briefing_tf_learning.json").write_text(
        json.dumps(
            {
                "generated_at": START.isoformat(),
                "per_timeframe": {
                    "15m": {
                        "evaluated": 1,
                        "hits": 1,
                        "flat": 0,
                        "tech_weight": 0.60,
                        "news_weight": 0.40,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ps_output = "\n".join(
        [
            "100 /bin/zsh ./fx_briefing_loop.sh",
            "101 /bin/zsh ./fx_tf_snapshot_loop.sh",
            "102 python3 tools/ai_learning_dashboard/server.py --port 8767",
        ]
    )

    state = server.build_state(tmp_path, now=START, ps_output=ps_output)
    ops = state["ops"]

    assert ops["status"] == "ok"
    assert ops["signals"]["has_any_journal"] is True
    assert ops["signals"]["has_timeframe_prices"] is True
    assert ops["signals"]["has_any_learning"] is True
    assert [process["running"] for process in ops["processes"]] == [True, True, True]
    assert ops["alerts"] == []
