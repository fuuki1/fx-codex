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


def test_learning_payload_preserves_zero_fusion_counts(server) -> None:
    result = server._learning_payload(
        {"evaluated": 1, "hits": 0, "flat": 0},
        {"evaluated": 1921, "hits": 930, "flat": 12},
        {},
        {"mode": "fusion", "label_ja": "融合1判断"},
    )

    assert result["evaluated"] == 1
    assert result["hits"] == 0
    assert result["flat"] == 0
    assert result["hit_rate"] == 0.0


def test_net_r_summary_reads_canonical_labels_without_recalculation(server) -> None:
    result = server._net_r_summary(
        {
            "outcomes": [
                {
                    "ts": "2026-07-01T00:00:00+00:00",
                    "decision_id": "d1",
                    "realized_r": 1.0,
                    "realized_net_r": 0.8,
                    "tradable": True,
                    "net_label_eligible": True,
                    "label_version": "net-r-v1",
                    "cost_model_id": "quotes-v1",
                },
                {
                    "ts": "2026-07-02T00:00:00+00:00",
                    "decision_id": "d2",
                    "realized_r": -1.0,
                    "realized_net_r": -1.2,
                    "tradable": True,
                    "net_label_eligible": True,
                    "label_version": "net-r-v1",
                    "cost_model_id": "quotes-v1",
                },
                {
                    "ts": "2026-07-03T00:00:00+00:00",
                    "realized_r": 0.2,
                    "realized_net_r": None,
                    "tradable": True,
                    "quality_flags": ["missing_net_label_entry_quote"],
                },
            ]
        }
    )

    assert result["labels"] == 2
    assert result["scored"] == 3
    assert result["coverage"] == pytest.approx(2 / 3)
    assert result["expectancy_r"] == pytest.approx(-0.2)
    assert result["cumulative_net_r"] == pytest.approx(-0.4)
    assert result["curve"][-1]["cumulative_net_r"] == pytest.approx(-0.4)
    assert result["missing_reasons"] == {"missing_net_label_entry_quote": 1}


def test_input_context_summary_reports_coverage_and_status(server) -> None:
    journal_rows = [
        {
            "timeframe": "15m",
            "input_context_id": "ctx-1",
            "input_feature_masks": {"macro__vix_level": 1},
        },
        {"timeframe": "1h", "input_context_id": "ctx-1", "input_feature_masks": {}},
        {"timeframe": "4h"},
    ]
    events = [
        {
            "decision": {
                "input_context_id": "ctx-1",
                "input_context": {
                    "context_id": "ctx-1",
                    "macro": {"quality_status": "partial"},
                    "liquidity": {
                        "status": "thin",
                        "features": {"spread_pips": 1.2},
                        "quote": {"source": "oanda_v20_pricing"},
                    },
                },
            }
        }
    ]

    result = server._input_context_summary(journal_rows, events)

    assert result["coverage"] == pytest.approx(2 / 3)
    assert result["unique_contexts"] == 1
    assert result["macro_status"] == {"partial": 1}
    assert result["liquidity_status"] == {"thin": 1}
    assert result["quote_sources"] == {"oanda_v20_pricing": 1}
    assert result["feature_coverage"][0]["coverage"] == 1.0


def test_learning_payload_exposes_session_and_regime_dimensions(server) -> None:
    result = server._learning_payload(
        {
            "evaluated": 10,
            "hits": 7,
            "dimension_stats": {
                "session_bucket": {
                    "london": {
                        "long": {
                            "raw": 10,
                            "evaluated": 10,
                            "hits": 7,
                            "flat": 0,
                            "hit_rate": 0.7,
                            "avg_move_atr": 0.2,
                        }
                    }
                },
                "regime": {
                    "risk_off": {"long": {"raw": 10, "evaluated": 10, "hits": 7, "flat": 0}}
                },
            },
        },
        {"evaluated": 0, "hits": 0, "flat": 0},
        {},
        {"mode": "fusion", "label_ja": "融合1判断"},
    )
    assert {row["dimension"] for row in result["dimensions"]} == {
        "session_bucket",
        "regime",
    }
    london = next(row for row in result["dimensions"] if row["bucket"] == "london")
    assert london["hit_rate"] == 0.7


def test_build_state_exposes_shadow_summary(server, tmp_path) -> None:
    shadow = {
        "schema": 1,
        "predictions": 4,
        "outcomes": 3,
        "by_producer": {"fusion_raw": {"effective": 3, "hits": 2, "hit_rate": 2 / 3}},
    }
    dimensions = {
        "regime": {
            "risk_off": {
                "long": {
                    "effective": 3,
                    "net_labels": 2,
                    "net_label_coverage": 2 / 3,
                    "net_expectancy_r": 0.2,
                    "cumulative_net_r": 0.4,
                }
            }
        }
    }
    (tmp_path / "briefing_decision_outcomes.json").write_text(
        json.dumps({"shadow_summary": shadow, "dimension_summary": dimensions}), encoding="utf-8"
    )
    state = server.build_state(tmp_path, now=START, ps_output="")
    assert state["shadow"] == shadow
    assert state["dimension_outcomes"][0]["net_expectancy_r"] == 0.2


def test_build_state_reports_missing_learning_ops_inputs(server, tmp_path) -> None:
    state = server.build_state(
        tmp_path,
        now=START,
        ps_output="",
        launchctl_outputs={},
    )
    ops = state["ops"]

    assert ops["status"] == "fail"
    assert ops["signals"]["has_any_journal"] is False
    assert ops["signals"]["has_timeframe_prices"] is False
    assert ops["signals"]["has_any_learning"] is False
    assert ops["processes"][0]["running"] is False
    assert ops["processes"][1]["running"] is False
    assert any("判断ログ" in alert["message_ja"] for alert in ops["alerts"])
    assert any("価格スナップショット" in alert["message_ja"] for alert in ops["alerts"])


def test_build_state_reports_running_learning_ops(server, tmp_path) -> None:
    (tmp_path / "briefing_journal.jsonl").write_text("{}\n", encoding="utf-8")
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

    launchctl_output = "state = waiting\nlast exit code = 0\n"
    state = server.build_state(
        tmp_path,
        now=START,
        ps_output=ps_output,
        launchctl_outputs={
            "com.fx-codex.snapshot": launchctl_output,
            "com.fx-codex.briefing": launchctl_output,
            "com.fx-codex.health": launchctl_output,
            "com.fx-codex.horizon": launchctl_output,
            "com.fx-codex.monitors": launchctl_output,
        },
    )
    ops = state["ops"]

    assert ops["status"] == "ok"
    assert ops["signals"]["has_any_journal"] is True
    assert ops["signals"]["has_timeframe_prices"] is True
    assert ops["signals"]["has_any_learning"] is True
    assert [process["running"] for process in ops["processes"]] == [
        True,
        True,
        True,
        True,
        True,
        True,
    ]
    assert state["journal"]["fusion_total"] == 1
    assert state["journal"]["timeframe_total"] == 1
    assert ops["alerts"] == []


def test_learning_curve_is_cumulative_over_scored_judgments(server) -> None:
    """学習の推移: 採点済み判断を古い順に累積した的中率と採点数を返す。

    各判断行は close を持つため将来価格系列の1点も兼ねる。ここでは1h足を
    08:00/09:00/10:00/11:00 に並べ、各行がその1時間後の行を将来価格として採点される
    (08:00→hit / 09:00→miss / 10:00→miss、11:00は将来価格が無く pending)。
    """
    entries = [
        _row(START, "1h", 1.0, "long", 156.0),  # →09:00で上昇 = hit
        _row(START + timedelta(hours=1), "1h", 1.0, "long", 156.4),  # →10:00で下落 = miss
        _row(START + timedelta(hours=2), "1h", 1.0, "long", 156.0),  # →11:00で下落 = miss
        _row(START + timedelta(hours=3), "1h", 1.0, "long", 155.5),  # 将来価格なし = pending
    ]
    result = server._evaluate_journal(entries)
    curve = result["curve"]
    # 採点済み(hit/miss)は3件。累積で1点ずつ増える
    assert [p["scored"] for p in curve] == [1, 2, 3]
    # hit→miss→miss なので累積hitsは 1 のまま
    assert [p["hits"] for p in curve] == [1, 1, 1]
    # 的中率は 100% → 50% → 33.3% と収束していく
    assert curve[0]["hit_rate"] == 1.0
    assert curve[1]["hit_rate"] == 0.5
    assert curve[2]["hit_rate"] == pytest.approx(0.3333, abs=1e-4)
    # 時系列順(古い順)に並ぶ
    assert [p["ts"] for p in curve] == sorted(p["ts"] for p in curve)


def test_learning_curve_excludes_flat(server) -> None:
    """小動き(flat)は的中率の分母に入らず、curve にも現れない。"""
    entries = [
        # 唯一の採点ペア。ATR10%(=0.05)以下の値動き → flat
        _row(START, "1h", 1.0, "long", 150.0, atr=0.5),
        _row(START + timedelta(hours=1), "1h", 1.0, "long", 150.01, atr=0.5),
    ]
    result = server._evaluate_journal(entries)
    # flat=1, 採点済み(hit/miss)=0 → curve は空
    assert result["flat"] == 1
    assert result["evaluated"] == 0
    assert result["curve"] == []


def test_learning_curve_accumulates_net_r_from_execution_cost(server) -> None:
    """execution_cost_r 付きの判断から、curve に累積純R(コスト控除後)が乗る。"""
    # long判断 close=100 atr=1.0 cost=0.15、1h後 close=101(+1R方向)→ 純R +0.85R
    entries = [
        {
            "ts": START.isoformat(),
            "symbol": "USDJPY",
            "timeframe": "1h",
            "horizon_hours": 1.0,
            "direction": "long",
            "action": "long",
            "conviction": 55,
            "close": 100.0,
            "atr": 1.0,
            "execution_cost_r": 0.15,
        },
        _row(START + timedelta(hours=1), "1h", 1.0, "long", 101.0, atr=1.0),
    ]
    result = server._evaluate_journal(entries)
    curve = result["curve"]
    assert curve, "採点済みが1件以上あるはず"
    last = curve[-1]
    # cum_net_r = move_atr(+1.0) - cost(0.15) = 0.85
    assert last["net_r_points"] >= 1
    assert last["cum_net_r"] == pytest.approx(0.85, abs=1e-4)


def test_learning_curve_net_r_absent_without_cost(server) -> None:
    """execution_cost_r が無い判断は純Rを算出しない(cum_net_r=0, net_r_points=0)。"""
    entries = [
        _row(START, "1h", 1.0, "long", 100.0, atr=1.0),
        _row(START + timedelta(hours=1), "1h", 1.0, "long", 101.0, atr=1.0),
    ]
    result = server._evaluate_journal(entries)
    curve = result["curve"]
    assert curve
    assert curve[-1]["net_r_points"] == 0
    assert curve[-1]["cum_net_r"] == 0.0


def test_build_state_exposes_horizon_matrix_and_promotion_metrics(server, tmp_path) -> None:
    horizon_row = {
        "ts": START.isoformat(),
        "symbol": "USDJPY",
        "horizon": "5m",
        "direction": "long",
        "conviction": 42,
        "p_up": 0.5,
        "p_down": 0.25,
        "p_flat": 0.25,
        "calibrated": False,
    }
    (tmp_path / "briefing_horizon_forecasts.jsonl").write_text(
        json.dumps(horizon_row) + "\n", encoding="utf-8"
    )
    (tmp_path / "briefing_horizon_learning.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "contract": "horizon-pit-v1",
                "generated_at": START.isoformat(),
                "gbdt_review_gate": "approved_pre_a2",
                "scored_total": 12,
                "profiles": {
                    "USDJPY|5m": {
                        "symbol": "USDJPY",
                        "horizon": "5m",
                        "n_scored": 12,
                        "hits": 7,
                        "misses": 5,
                        "hit_rate": 7 / 12,
                        "mean_brier": 0.55,
                        "mean_log_loss": 0.9,
                        "band_coverage": 0.8,
                        "mean_net_r": 0.1,
                        "promotion": {
                            "stage": "shadow",
                            "permanent_shadow": True,
                            "remaining_n": None,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state = server.build_state(tmp_path, now=START, ps_output="")

    assert state["horizon"]["contract"] == "horizon-pit-v1"
    assert state["horizon"]["journal_rows"] == 1
    assert state["horizon"]["latest"][0]["horizon"] == "5m"
    assert state["horizon"]["profiles"][0]["permanent_shadow"] is True
    assert state["ops"]["signals"]["has_horizon_track"] is True


def test_learning_payload_passes_counterfactual_count_for_fusion(server):
    """期待値ガード反実仮想の採点件数がフロントへ渡ること(運用○×と分離表示するため)。"""
    learning = {
        "generated_at": START.isoformat(),
        "evaluated": 21,
        "hits": 6,
        "flat": 0,
        "counterfactual_evaluated": 8,
        "notes_ja": [],
    }
    evaluated = {"evaluated": 0, "hits": 0, "flat": 0}
    payload = server._learning_payload(learning, evaluated, {}, {"mode": "fusion"})
    assert payload["counterfactual_evaluated"] == 8

    without = dict(learning)
    del without["counterfactual_evaluated"]
    payload = server._learning_payload(without, evaluated, {}, {"mode": "fusion"})
    assert payload["counterfactual_evaluated"] == 0


def _pit(row: dict) -> dict:
    prediction = datetime.fromisoformat(row["ts"])
    return {
        **row,
        "prediction_time": prediction.isoformat(),
        "source_cutoff": (prediction - timedelta(minutes=2)).isoformat(),
        "max_feature_available_time": (prediction - timedelta(seconds=1)).isoformat(),
        "pit_eligible": True,
    }


def test_build_state_excludes_legacy_fusion_rows_from_gbdt(server, tmp_path) -> None:
    rows = [
        _row(START, "", 24.0, "long", 150.0),
        _row(START + timedelta(hours=24), "", 24.0, "long", 151.0),
    ]
    (tmp_path / "briefing_journal.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    training = server.build_state(tmp_path)["ml"]["training"]

    assert training["evaluated"] == 0
    assert training["eligible_after_thinning"] == 0
    assert training["pit_ineligible"] == 2


def test_build_state_fails_when_one_required_journal_is_missing(server, tmp_path) -> None:
    (tmp_path / "briefing_journal.jsonl").write_text(
        json.dumps(_row(START, "", 24.0, "long", 150.0)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "briefing_tf_prices.jsonl").write_text(
        json.dumps(_row(START, "15m", 0.25, "neutral", 150.0)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "briefing_learning.json").write_text("{}", encoding="utf-8")
    launchctl_outputs = {
        label: "state = not running\nlast exit code = 0\n"
        for _, _, label in server.LAUNCHD_SERVICES
    }

    state = server.build_state(
        tmp_path,
        now=START,
        ps_output="",
        launchctl_outputs=launchctl_outputs,
    )

    assert state["ops"]["status"] == "fail"
    assert any("時間足別判断ログが未作成" in row["message_ja"] for row in state["ops"]["alerts"])


def test_build_state_prefers_scored_timeframe_learning_over_empty_fusion_profile(
    server,
    tmp_path,
) -> None:
    (tmp_path / "briefing_learning.json").write_text(
        json.dumps({"generated_at": START.isoformat(), "evaluated": 0, "hits": 0}),
        encoding="utf-8",
    )
    (tmp_path / "briefing_tf_learning.json").write_text(
        json.dumps(
            {
                "generated_at": START.isoformat(),
                "per_timeframe": {
                    "15m": {
                        "generated_at": START.isoformat(),
                        "evaluated": 3,
                        "hits": 2,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state = server.build_state(tmp_path)

    assert state["learning_source"]["mode"] == "timeframe"
    assert state["learning"]["evaluated"] == 3


def test_build_state_reports_briefing_notification_failure(server, tmp_path) -> None:
    launchctl_outputs = {
        label: (
            "state = not running\nlast exit code = 5\n"
            if label == "com.fx-codex.briefing"
            else "state = not running\nlast exit code = 0\n"
        )
        for _, _, label in server.LAUNCHD_SERVICES
    }

    state = server.build_state(
        tmp_path,
        now=START,
        ps_output="",
        launchctl_outputs=launchctl_outputs,
    )

    assert any("Discord通知" in row["message_ja"] for row in state["ops"]["alerts"])
    briefing = next(row for row in state["ops"]["processes"] if row["key"] == "briefing_service")
    assert briefing["running"] is True
    assert briefing["last_exit_code"] == 5


def test_build_state_reports_fusion_only_gbdt_training_progress(server, tmp_path) -> None:
    fusion_rows = [
        _pit(_row(START, "", 24.0, "long", 150.0)),
        _pit(_row(START + timedelta(hours=24), "", 24.0, "long", 151.0)),
    ]
    timeframe_rows = [
        _row(START, "15m", 0.25, "long", 150.0),
        _row(START + timedelta(minutes=15), "15m", 0.25, "long", 151.0),
    ]
    (tmp_path / "briefing_journal.jsonl").write_text(
        "\n".join(json.dumps(row) for row in fusion_rows) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "briefing_tf_journal.jsonl").write_text(
        "\n".join(json.dumps(row) for row in timeframe_rows) + "\n",
        encoding="utf-8",
    )

    state = server.build_state(tmp_path)
    training = state["ml"]["training"]

    assert state["evaluation"]["evaluated"] == 2
    assert training["evaluated"] == 1
    assert training["eligible_after_thinning"] == 1
    assert training["minimum_required"] == 150
    assert training["source"] == "briefing_journal.jsonl"
    assert training["pit_ineligible"] == 0


def test_dashboard_tones_launchd_exit_codes() -> None:
    script = (_SERVER_PATH.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'exitCode === 5 ? "warn" : "fail"' in script


def test_gbdt_progress_thins_flat_before_dropping_it(server) -> None:
    outcomes = [
        {
            "ts": START.isoformat(),
            "symbol": "USDJPY",
            "outcome": "flat",
            "pit_eligible": True,
        },
        {
            "ts": (START + timedelta(hours=1)).isoformat(),
            "symbol": "USDJPY",
            "outcome": "hit",
            "pit_eligible": True,
        },
    ]

    assert server._thinned_outcome_count(outcomes) == 0


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


def test_ml_summary_rejects_pre_pit_artifact(server) -> None:
    summary = server._ml_summary(
        {
            "schema": 3,
            "trained_at": START.isoformat(),
            "usable": True,
            "model": {"trees": []},
        }
    )

    assert summary["has_model"] is False
    assert summary["usable"] is False
    assert any("旧PIT契約" in reason for reason in summary["reasons"])


def test_recent_outcomes_grouped_per_timeframe_with_cap(server) -> None:
    """時間足タブ用: 時間足ごとに直近12件ずつ返し、全体20件制限に潰されない。"""
    entries = []
    # 15mの判断を15件(12件制限を超えさせる)+ 1hの判断を2件
    for index in range(15):
        base = START + timedelta(minutes=20 * index)
        entries.append(_row(base, "15m", 0.25, "long", 150.0 + index, atr=0.05))
        entries.append(
            _row(base + timedelta(minutes=15), "15m", 0.25, "long", 150.2 + index, atr=0.05)
        )
    entries.append(_row(START, "1h", 1.0, "long", 156.0))
    entries.append(_row(START + timedelta(hours=1), "1h", 1.0, "short", 156.4))
    result = server._evaluate_journal(entries)
    grouped = result["recent_outcomes_by_timeframe"]
    assert set(grouped) == {"15m", "1h"}
    assert len(grouped["15m"]) == 12  # 直近12件に丸める
    assert all(row["timeframe"] == "15m" for row in grouped["15m"])
    # 旧UI互換のフラットな直近20件も残す
    assert len(result["recent_outcomes"]) <= 20
    # グループ内は時系列順(最後が最新)
    assert grouped["15m"][-1]["ts"] > grouped["15m"][0]["ts"]


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


