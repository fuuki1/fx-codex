from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path

import pytest

from fx_intel.virtual_portfolio import (
    VirtualPortfolioPolicy,
    open_virtual_portfolio_writer,
)

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "tools" / "ai_learning_dashboard" / "server.py"
INDEX_PATH = ROOT / "tools" / "ai_learning_dashboard" / "static" / "index.html"
APP_PATH = ROOT / "tools" / "ai_learning_dashboard" / "static" / "app.js"
READ_API_PATH = ROOT / "tools" / "virtual_portfolio_read_api.py"
START = datetime(2026, 7, 29, tzinfo=UTC)


def _server_module():
    spec = importlib.util.spec_from_file_location("virtual_portfolio_dashboard_server", SERVER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_api_module():
    spec = importlib.util.spec_from_file_location("virtual_portfolio_read_api", READ_API_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_state_reads_virtual_portfolio_from_fixed_path(tmp_path) -> None:
    database = tmp_path / "virtual.sqlite3"
    with open_virtual_portfolio_writer(database, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)

    state = _server_module().build_state(
        tmp_path,
        portfolio_db=database,
        now=START,
        ps_output="",
        launchctl_outputs={},
    )

    portfolio = state["virtual_portfolio"]
    assert portfolio["configured"] is True
    assert portfolio["balance_jpy"] == 1_000_000
    assert portfolio["daily_target_jpy"] == 70_000
    assert portfolio["execution_mode"] == "offline_simulation"
    assert portfolio["broker_connected"] is False
    assert portfolio["performance_claim_status"] == "evaluation_unavailable"
    assert portfolio["daily_profit_task"]["required"] is True
    assert portfolio["daily_profit_task"]["status"] == "in_progress"
    assert portfolio["daily_profit_task"]["target_jpy"] == 70_000
    assert portfolio["horizon_allocation"]["policy"]["capacity_timeframes"] == [
        "15m",
        "1h",
    ]
    assert portfolio["research_kpis"]["daily_profit_reference"]["required"] is True
    assert portfolio["research_kpis"]["daily_profit_reference"]["optimization_input"] is False


def test_dashboard_defaults_to_virtual_portfolio_and_has_no_live_stage() -> None:
    index = INDEX_PATH.read_text(encoding="utf-8")
    app = APP_PATH.read_text(encoding="utf-8")

    assert 'data-view="portfolio"' in index
    assert "仮想口座と純損益" in index
    assert "判断キュー" in index
    assert "実時間・paper約定ではありません" in index
    assert "research / validated / shadow" in index
    assert "shadow / paper / live" not in index
    assert 'id="portfolioCurveCanvas"' in index
    assert 'id="portfolioTradeSearch"' in index
    assert 'id="logDir"' not in index
    assert '<script src="portfolio.js"></script>' not in index
    assert 'let initialView = "portfolio"' in app
    assert 'portfolioOnly ? "/api/virtual-portfolio" : "/api/state"' in app
    assert "delayed bid/ask replay / broker disconnected" in app
    assert "反実仮想（因果断定なし）" in app
    assert "日次決済気配待ち" in app
    assert "実行可能評価あり" in app
    assert "portfolio.unrealized_pnl_jpy" in app
    assert "現在評価額（含み込み）" in index
    assert "確定残高（決済済み）" in index
    assert "現在評価額 ＝ 確定残高 ＋ 含み損益" in index
    assert "20日ローリング純R" in index
    assert "1日7万円は必須タスク" in index
    assert "本日確定損益（必須タスク）" in index
    assert "未達は継続表示 / 安全上限を優先 / 達成保証ではありません" in index
    assert "daily_profit_task" in app
    assert "必須タスク進行中" in app
    assert "portfolio.today_pnl_jpy" in app
    assert "15m/1hのみポリシー上限内 / 4h/1dは観測専用" in index
    assert 'id="portfolioOpenCount">0 / --<' in index
    assert "horizon_allocation?.capacity_limit" in app
    assert 'id="portfolioCanonicalNetR"' in index
    assert "正準ラベル接続エラー" in app
    assert "bid/ask・全費用後 / research-only" in app
    assert "目標線 1,070,000円" not in index
    assert "balanceY(target)" not in app
    assert "rolling_20d" in app
    assert "日次決済ライフサイクル不整合" in app
    assert "portfolio.equity_drawdown_jpy" in app
    assert "precisePct(portfolio.equity_drawdown_pct)" in app
    assert "logDir" not in app


def test_virtual_portfolio_api_is_loopback_query_only() -> None:
    server = _server_module()
    read_api = _read_api_module()

    assert read_api.LOOPBACK_HOSTS == {"127.0.0.1", "::1", "localhost"}
    assert read_api.VirtualPortfolioReadHandler.do_GET is not None
    assert not hasattr(read_api.VirtualPortfolioReadHandler, "do_POST")
    assert server._validated_read_api_url("http://127.0.0.1:8771") == ("http://127.0.0.1:8771")
    with pytest.raises(ValueError, match="loopback"):
        server._validated_read_api_url("http://100.118.242.40:8771")


def test_virtual_portfolio_proxy_timeout_covers_observed_snapshot_latency() -> None:
    """portfolio_snapshot() は CPU 律速で実機負荷に依存する。

    2026-08-05 の実機実測 (load 6-9) では ttfb が 7.0-30.1 秒に分布しており、
    旧実装の 5 秒固定タイムアウトでは全リクエストが失敗して
    /api/virtual-portfolio が常時 503 を返していた。短い値へ戻す退行を防ぐ。
    """
    server = _server_module()

    assert server.VIRTUAL_PORTFOLIO_READ_TIMEOUT_SECONDS >= 30.0


def test_dashboard_state_endpoint_reads_only_prebuilt_cache(tmp_path) -> None:
    server = _server_module()
    cache = tmp_path / "dashboard_state_cache.json"
    payload = {
        "generated_at": START.isoformat(),
        "dashboard_cache": {
            "contract": "fx-dashboard-state-cache-v1",
            "read_only": True,
        },
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")

    assert server._dashboard_state_cache(cache) == payload
    with pytest.raises(RuntimeError, match="not initialized"):
        server._dashboard_state_cache(tmp_path / "missing.json")
