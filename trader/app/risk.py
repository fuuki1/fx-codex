"""② リスク管理・フィルタ。

Redis Consumer Group `risk` で `signals` を購読し、全チェック通過分だけ
`orders` へ流す。判断の本体は純粋な risk_engine に委譲し、ここでは I/O（状態収集・
Kill switch・通知・レート制限・発注サイズの確定）を担う。

判断順（risk_engine.evaluate）:
  1. イベント・ブラックアウト  2. 取引セッション  3. 日次損失  4. 週次損失
  5. 連敗停止  6. リスク基準サイジング  7. 数量上限  8. 同時保有数  9. 通貨エクスポージャ
risk.py 側の追加ガード:
  - Kill switch（Redis, fail-safe）を最初に確認（kill なら DB に触れず即却下）。
  - 承認後に発注レート（Redis 永続スライディングウィンドウ）を最終確認。
  - 日次／週次／連敗停止のときは Kill switch を自動 ON ＋通知。
"""
from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common
import risk_engine
from config import settings
from domain import rate_limit_allow
from logging_setup import log_extra, set_correlation_id, setup_logging

log = setup_logging("risk", settings.log_level)

GROUP = "risk"
CONSUMER = f"{socket.gethostname()}-{os.getpid()}"
RATE_KEY = "rate:orders"


# ============================================================================
# 設定 → RiskParams
# ============================================================================
def build_params() -> risk_engine.RiskParams:
    return risk_engine.RiskParams(
        sizing_enabled=settings.risk_sizing_enabled,
        account_equity=settings.account_equity,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        require_stop_for_sizing=settings.require_stop_for_sizing,
        lot_step=settings.lot_step,
        min_lot=settings.min_lot,
        max_position_qty=settings.max_position_qty,
        max_daily_loss=settings.max_daily_loss_jpy,
        max_weekly_loss=settings.max_weekly_loss_jpy,
        loss_streak_reduce_at=settings.loss_streak_reduce_at,
        loss_streak_reduce_factor=settings.loss_streak_reduce_factor,
        loss_streak_halt_at=settings.loss_streak_halt_at,
        max_concurrent_positions=settings.max_concurrent_positions,
        max_currency_exposure=settings.max_currency_exposure,
        enforce_session=settings.enforce_session,
    )


# ============================================================================
# 状態収集（DB / ファイル）。各関数は monkeypatch しやすいよう独立させる。
# ============================================================================
def _day_pnl() -> float:
    rows = common.db_query(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM fills WHERE ts >= date_trunc('day', now())"
    )
    return float(rows[0][0]) if rows else 0.0


def _week_pnl() -> float:
    rows = common.db_query(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM fills WHERE ts >= date_trunc('week', now())"
    )
    return float(rows[0][0]) if rows else 0.0


def _recent_pnls(limit: int) -> list[float]:
    """直近の「確定した（realized_pnl != 0）」トレード損益を新しい順で返す。"""
    rows = common.db_query(
        "SELECT realized_pnl FROM fills WHERE realized_pnl <> 0 ORDER BY ts DESC LIMIT %s",
        (limit,),
    )
    return [float(r[0]) for r in rows]


def _open_positions() -> list[tuple[str, float]]:
    """fills から銘柄ごとの純建玉（BUY=+ / SELL=-）を推定して返す（フラットは除外）。

    注意: fills は発注の追記ログ。約定の純額を厳密に持たないため「発注の符号付き合計」を
    現在ポジションの近似とする（フラット⇄ロング/ショートに反転する戦略では正確）。
    厳密な突合は reconcile（ブローカー実ポジション）に委ねる。
    """
    rows = common.db_query(
        "SELECT symbol, "
        "SUM(CASE WHEN upper(side)='BUY' THEN qty ELSE -qty END) AS net "
        "FROM fills GROUP BY symbol HAVING SUM(CASE WHEN upper(side)='BUY' THEN qty ELSE -qty END) <> 0"
    )
    return [(str(r[0]), float(r[1])) for r in rows]


# ---- イベント・ブラックアウト・カレンダー（ファイル / ホットリロード）----------
class BlackoutCalendar:
    """重要指標ブラックアウト窓を JSON から読む（mtime 監視でホットリロード）。

    形式: {"windows": [{"start": ISO, "end": ISO, "label": "US CPI"}, ...]}
    ファイルが無ければ窓ゼロ（＝ブラックアウト無効）。strategy_params と同じ運用感。
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._mtime: float = -1.0
        self.windows: list[tuple[float, float, str]] = []

    def get(self) -> list[tuple[float, float, str]]:
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self.windows = _parse_blackouts(json.loads(self.path.read_text()))
                self._mtime = mtime
                log.info("blackout calendar loaded", **log_extra(windows=len(self.windows)))
        except FileNotFoundError:
            if self._mtime != -1.0:  # 一度読めていたが消えた
                self.windows = []
                self._mtime = -1.0
        except Exception:
            log.exception("failed to load blackout calendar; keeping previous")
        return self.windows


def _parse_blackouts(doc: dict[str, Any]) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for w in doc.get("windows", []):
        try:
            start = _iso(w["start"])
            end = _iso(w["end"])
        except (KeyError, ValueError):
            log.warning("skip invalid blackout window", **log_extra(window=w))
            continue
        if end < start:
            start, end = end, start
        out.append((start, end, str(w.get("label", "blackout"))))
    return out


def _iso(value: str) -> float:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


_calendar = BlackoutCalendar(settings.risk_blackout_file)


def _value_per_point(symbol: str) -> float:
    return settings.risk_value_per_point.get(symbol.upper(), 1.0)


def gather_state(sig: dict[str, Any]) -> risk_engine.RiskState:
    """発注判断に必要な外部状態を一度に集める。

    DB 不通などで集められない場合は例外を投げる（呼び出し側で fail-closed に扱う）。
    """
    symbol = str(sig.get("symbol", ""))
    return risk_engine.RiskState(
        now=datetime.now(UTC),
        day_pnl=_day_pnl(),
        week_pnl=_week_pnl(),
        recent_pnls=_recent_pnls(settings.recent_trades_window),
        open_positions=_open_positions(),
        blackout_windows=_calendar.get(),
        value_per_point=_value_per_point(symbol),
    )


# ============================================================================
# 判断（I/O + 純粋エンジン）
# ============================================================================
def decide(sig: dict[str, Any]) -> risk_engine.RiskDecision:
    """1 シグナルの最終判断。副作用（Kill switch/通知/レート制限）もここで行う。"""
    set_correlation_id(sig.get("idem"))

    # 0) Kill switch（Redis, fail-safe）。ON なら DB に触れず即却下。
    if common.kill_switch_on():
        _log_reject(sig, "kill_switch_on")
        return risk_engine.RiskDecision(approved=False, reason="kill_switch_on")

    # 1) 状態収集 → 純粋エンジンで判断
    try:
        state = gather_state(sig)
    except Exception:
        # 必要なデータを読めないなら「承認しない」（fail-closed）。consume 側で再試行される。
        log.exception("gather_state failed -> reject (fail-closed)", **log_extra(idem=sig.get("idem")))
        raise
    decision = risk_engine.evaluate(sig, state, build_params())

    # 2) 強制停止（日次/週次/連敗）は Kill switch を自動 ON ＋通知
    if decision.trip_kill_switch:
        common.set_kill_switch(True, reason=decision.reason)
        common.notify(_kill_message(decision), key=f"risk_halt:{decision.reason}")

    if not decision.approved:
        _log_reject(sig, decision.reason, **decision.details)
        return decision

    # 3) 承認後の最終ガード: 発注レート（Redis 永続スライディングウィンドウ）
    if not rate_limit_allow(common.r(), RATE_KEY, settings.max_orders_per_min):
        _log_reject(sig, "rate_limited", limit_per_min=settings.max_orders_per_min)
        return risk_engine.RiskDecision(approved=False, reason="rate_limited")

    common.log_event("risk_decision", {
        "decision": "approve", "signal": sig,
        "sized_qty": decision.sized_qty, "intended_risk": decision.intended_risk,
        "loss_streak": decision.loss_streak,
    })
    return decision


def evaluate(sig: dict[str, Any]) -> bool:
    """後方互換の真偽判定（テスト・簡易呼び出し用）。"""
    return decide(sig).approved


def _log_reject(sig: dict[str, Any], reason: str, **fields: Any) -> None:
    log.warning("rejected: %s", reason, **log_extra(idem=sig.get("idem"), reason=reason, **fields))
    common.log_event("risk_decision", {"decision": "reject", "reason": reason, "signal": sig})


def _kill_message(d: risk_engine.RiskDecision) -> str:
    if d.reason == risk_engine.R_DAILY_LOSS:
        return (
            f"🛑 日次損失が上限を超過（{d.details.get('day_pnl', 0):.0f} "
            f"<= -{d.details.get('limit', 0):.0f}）。Kill switch を自動 ON。"
        )
    if d.reason == risk_engine.R_WEEKLY_LOSS:
        return (
            f"🛑 週次損失が上限を超過（{d.details.get('week_pnl', 0):.0f} "
            f"<= -{d.details.get('limit', 0):.0f}）。Kill switch を自動 ON（翌週まで停止）。"
        )
    if d.reason == risk_engine.R_LOSS_STREAK:
        return (
            f"🛑 {d.details.get('streak')} 連敗に到達。新規を停止しレビューへ。"
            f"Kill switch を自動 ON（点検後に手動解除）。"
        )
    return f"🛑 リスク強制停止: {d.reason}"


def handle(sig: dict[str, Any]) -> None:
    decision = decide(sig)
    if not decision.approved:
        return
    # サイジング結果（数量）と想定リスク額を注文に載せて executor へ。
    order = {
        **sig,
        "qty": decision.sized_qty,
        "intended_risk": decision.intended_risk,
    }
    common.publish(common.STREAM_ORDERS, order)
    log.info(
        "approved -> orders",
        **log_extra(
            idem=sig.get("idem"), symbol=sig.get("symbol"),
            qty=decision.sized_qty, intended_risk=round(decision.intended_risk, 2),
        ),
    )


def main() -> None:
    stop = common.install_signal_handlers()
    log.info(
        "risk service starting",
        **log_extra(consumer=CONSUMER, sizing=settings.risk_sizing_enabled),
    )
    common.consume(common.STREAM_SIGNALS, GROUP, CONSUMER, handle, stop, service="risk")
    log.info("risk service stopped")


if __name__ == "__main__":
    main()
