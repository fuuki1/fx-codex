"""② リスク管理・フィルタ。

Redis Consumer Group `risk` で `signals` を購読し、全チェック通過分だけ
`orders` へ流す。チェックは上から順に、1 つでも引っかかれば却下。

  1. Kill switch
  2. 数量上限（MAX_POSITION_QTY, 1 注文あたり）
  3. ストップロス必須（REQUIRE_STOP_LOSS, close=true は免除）
  4. 銘柄許可リスト（SYMBOL_ALLOWLIST）
  5. 純建玉上限（MAX_NET_POSITION_QTY, 建玉を増やす方向のみ制限）
  6. 取引時間帯（within_session, ENFORCE_SESSION のとき）
  7. 日次損失（JST 日界、超過で自動 Kill switch ON + 通知）
  7.5 リスクエンジン（RISK_ENGINE_MODE: off/shadow/enforce。既存チェックへの追加層。
      週次損失・DD停止・連敗スロットル・サイジング・同時保有・通貨エクスポージャ・
      イベント/薄商いブラックアウト・R:R 下限。shadow は観測ログのみで発注に影響しない）
  8. 発注レート（MAX_ORDERS_PER_MIN, Redis 永続のスライディングウィンドウ）
"""
from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import common
import risk_engine
from config import settings
from domain import rate_limit_allow, within_session
from logging_setup import log_extra, set_correlation_id, setup_logging

log = setup_logging("risk", settings.log_level)

GROUP = "risk"
CONSUMER = f"{socket.gethostname()}-{os.getpid()}"
RATE_KEY = "rate:orders"


def _today_realized_pnl() -> float:
    """当日（JST 日界）の実現損益。

    DB セッションは UTC なので素の date_trunc('day', now()) だと「日次」が
    朝 9 時 JST でリセットされてしまう。日次損失の集計窓は JST の 0 時に固定する。
    """
    rows = common.db_query(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM fills "
        "WHERE ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'Asia/Tokyo'"
    )
    return float(rows[0][0]) if rows else 0.0


def _net_position(symbol: str) -> float:
    """fills の累積からシンボルの純建玉を推定する（BUY=+qty / SELL=-qty）。

    fills は「発注記録」なので拒否・部分約定分を過大に数える可能性があるが、
    エクスポージャー制限としては安全側（実際より大きく見積もる）。
    実建玉との突合は reconcile が担う。
    """
    rows = common.db_query(
        "SELECT COALESCE(SUM(CASE WHEN side = 'BUY' THEN qty ELSE -qty END), 0) "
        "FROM fills WHERE symbol = %s",
        (symbol,),
    )
    return float(rows[0][0]) if rows else 0.0


def _reject(sig: dict[str, Any], reason: str, **fields: Any) -> None:
    log.warning("rejected: %s", reason, **log_extra(idem=sig.get("idem"), reason=reason, **fields))
    common.log_event("risk_decision", {"decision": "reject", "reason": reason, "signal": sig})


# ============================================================================
# リスクエンジン統合（RISK_ENGINE_MODE: off | shadow | enforce）
#
# 既存チェック 1〜8 は一切変更せず、その「後段」に純粋エンジン risk_engine.evaluate を
# 追加する。off は評価すら行わず従来と完全同一。shadow は判断を risk_engine_decision
# イベントに記録するだけで発注へ影響しない（enforce 昇格前の観測用）。enforce で初めて
# 却下・サイジング・Kill switch 連動が効く。
# ============================================================================
KEY_PNL_HWM = "risk:pnl_hwm"  # 実現損益の高値（high-water mark）。ドローダウン算出に使う。

# 集計境界を「設定 timezone のその日/週の開始」に合わせる（既定 JST）。
# 既存チェック 7 の _today_realized_pnl と同じ手法の period/timezone パラメータ版
# （既存側は挙動固定のため触らない）。
_PNL_SINCE_LOCAL = (
    "SELECT COALESCE(SUM(realized_pnl), 0) FROM fills "
    "WHERE ts >= date_trunc(%s, now() AT TIME ZONE %s) AT TIME ZONE %s"
)


def _pnl_since_local(period: str) -> float:
    tz = settings.risk_day_timezone
    rows = common.db_query(_PNL_SINCE_LOCAL, (period, tz, tz))
    return float(rows[0][0]) if rows else 0.0


def _day_pnl() -> float:
    return _pnl_since_local("day")


def _week_pnl() -> float:
    return _pnl_since_local("week")


def _recent_pnls(limit: int) -> list[float]:
    """直近の「確定した（realized_pnl != 0）」トレード損益を新しい順で返す。"""
    rows = common.db_query(
        "SELECT realized_pnl FROM fills WHERE realized_pnl <> 0 ORDER BY ts DESC LIMIT %s",
        (limit,),
    )
    return [float(r[0]) for r in rows]


def _cumulative_pnl() -> float:
    """全期間の累計実現損益（ドローダウンの基準となる実現エクイティ）。"""
    rows = common.db_query("SELECT COALESCE(SUM(realized_pnl), 0) FROM fills")
    return float(rows[0][0]) if rows else 0.0


def _equity_drawdown() -> float:
    """累計実現損益の高値（HWM）からの現在ドローダウン額（>=0）。max_drawdown_pct=0 で 0。

    HWM（ピーク）を Redis に保持し、毎回 max(HWM, 現在の累計) を書き戻して初期化・更新する。
    cum を全期間で取る（窓を切らない）ことで、古い利益が期間外へ抜けて DD が誤って膨らむのを防ぐ。
    """
    if settings.max_drawdown_pct <= 0:
        return 0.0
    cum = _cumulative_pnl()
    try:
        # r() は decode_responses=True の同期クライアント（common.r の規約）なので str が返る
        stored = cast(str | None, common.r().get(KEY_PNL_HWM))
        hwm = max(float(stored), cum) if stored is not None else cum
        common.r().set(KEY_PNL_HWM, hwm)
    except Exception:
        # Redis 不通時は DD 判定を見送る（best-effort）。Redis 断は evaluate 冒頭の
        # kill_switch_on() が既に fail-safe で発注を止めているため、ここは 0 で足りる。
        log.exception("equity drawdown: redis HWM access failed -> skip DD check")
        return 0.0
    return max(hwm - cum, 0.0)


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


def build_params() -> risk_engine.RiskParams:
    """設定 → RiskParams。セッションは既存チェック 6 が担当するため二重評価しない。"""
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
        min_reward_risk=settings.min_reward_risk,
        require_target_for_rr=settings.require_target_for_rr,
        require_reason=settings.require_reason,
        max_drawdown=settings.max_drawdown_pct / 100.0 * settings.account_equity,
        enforce_session=False,
    )


def gather_state(sig: dict[str, Any]) -> risk_engine.RiskState:
    """発注判断に必要な外部状態を一度に集める。

    DB 不通などで集められない場合は例外を投げる（enforce では fail-closed、
    shadow では本流を止めずスキップ）。
    """
    symbol = str(sig.get("symbol", ""))
    return risk_engine.RiskState(
        now=datetime.now(UTC),
        day_pnl=_day_pnl(),
        week_pnl=_week_pnl(),
        recent_pnls=_recent_pnls(settings.recent_trades_window),
        open_positions=_open_positions(),
        blackout_windows=_calendar.get(),
        thin_liquidity_windows=settings.thin_liquidity_windows,
        equity_drawdown=_equity_drawdown(),
        value_per_point=_value_per_point(symbol),
    )


def _engine_check(sig: dict[str, Any], qty: float) -> tuple[bool, float]:
    """リスクエンジンを評価し (発注可否, 発注数量) を返す。

    shadow では常に (True, qty) を返し、判断は risk_engine_decision イベントに残すだけ。
    enforce では却下／Kill switch 連動／サイジング済み数量の適用を行う。
    """
    mode = settings.risk_engine_mode
    try:
        state = gather_state(sig)
    except Exception:
        if mode == "enforce":
            # 状態を読めないのに発注を通さない（fail-closed）
            log.exception("risk_engine: gather_state failed -> reject (fail-closed)",
                          **log_extra(idem=sig.get("idem")))
            _reject(sig, "risk_engine_state_unavailable")
            return False, qty
        log.exception("risk_engine(shadow): gather_state failed -> skip",
                      **log_extra(idem=sig.get("idem")))
        return True, qty

    # 現行スキーマの決済フラグ（close=true）をエンジンの EXIT_INTENTS へ橋渡しする。
    # 決済はリスク削減なので入口ゲート（ブラックアウト等）を課さない（手仕舞いを妨げない）。
    engine_sig = dict(sig)
    if engine_sig.get("close"):
        engine_sig.setdefault("intent", "exit")
    decision = risk_engine.evaluate(engine_sig, state, build_params())

    common.log_event("risk_engine_decision", {
        "mode": mode,
        "approved": decision.approved,
        "reason": decision.reason,
        "requested_qty": qty,
        "sized_qty": decision.sized_qty,
        "intended_risk": decision.intended_risk,
        "loss_streak": decision.loss_streak,
        "trip_kill_switch": decision.trip_kill_switch,
        "details": decision.details,
        "idem": sig.get("idem"),
        "symbol": sig.get("symbol"),
    })

    if mode != "enforce":
        if not decision.approved:
            log.info("risk_engine(shadow) would reject",
                     **log_extra(idem=sig.get("idem"), reason=decision.reason))
        return True, qty

    if decision.trip_kill_switch:
        common.set_kill_switch(True, reason=decision.reason)
        common.notify(
            f"🛑 リスクエンジンが強制停止: {decision.reason}"
            f"（連敗{decision.loss_streak}）。Kill switch を自動 ON。",
            key=f"risk_halt:{decision.reason}",
        )
    if not decision.approved:
        _reject(sig, decision.reason, **decision.details)
        return False, qty

    if decision.sized_qty > 0 and decision.sized_qty != qty:
        log.info("risk_engine sized qty",
                 **log_extra(idem=sig.get("idem"), requested=qty, sized=decision.sized_qty))
        return True, decision.sized_qty
    return True, qty


def evaluate(sig: dict[str, Any]) -> bool:
    """True を返したら発注可。副作用としてイベントログ/通知/KillSwitch を行う。"""
    set_correlation_id(sig.get("idem"))

    # 1) Kill switch
    if common.kill_switch_on():
        _reject(sig, "kill_switch_on")
        return False

    # 2) 数量上限（1 注文あたり）
    qty = float(sig.get("qty", 0))
    if qty > settings.max_position_qty:
        _reject(sig, "qty_over_limit", qty=qty, limit=settings.max_position_qty)
        return False

    # 3) ストップロス必須（含み損を無制限に放置するポジションを作らない）。
    #    決済シグナル（close=true）は建玉を減らす方向なので免除。
    if (
        settings.require_stop_loss
        and not sig.get("close")
        and not (sig.get("stop_price") or sig.get("stop_distance"))
    ):
        _reject(sig, "stop_loss_required")
        return False

    # 4) 銘柄許可リスト（secret 漏洩・設定ミスで任意銘柄へ発注されるのを防ぐ）
    symbol = str(sig.get("symbol", "")).upper()
    if settings.symbol_allowlist and symbol not in settings.symbol_allowlist:
        _reject(sig, "symbol_not_allowed", symbol=symbol)
        return False

    # 5) 純建玉上限（建玉を「増やす」発注のみ制限。決済方向は常に通す）
    signed_qty = qty if sig.get("side") == "BUY" else -qty
    net = _net_position(symbol)
    projected = net + signed_qty
    increases_exposure = abs(projected) > abs(net)
    if increases_exposure and abs(projected) > settings.max_net_position_qty:
        _reject(
            sig,
            "net_position_over_limit",
            net=net,
            projected=projected,
            limit=settings.max_net_position_qty,
        )
        common.notify(
            f"⚠️ 純建玉上限で却下: {symbol} 現在{net:g} → 発注後{projected:g} "
            f"(上限 {settings.max_net_position_qty:g})。シグナル連打の可能性を確認。",
            key=f"net_position:{symbol}",
        )
        return False

    # 6) 取引時間帯
    if settings.enforce_session and not within_session(sig.get("asset", ""), sig.get("symbol", "")):
        _reject(sig, "out_of_session", asset=sig.get("asset"), symbol=sig.get("symbol"))
        return False

    # 7) 日次損失
    pnl = _today_realized_pnl()
    if pnl <= -abs(settings.max_daily_loss_jpy):
        common.set_kill_switch(True, reason="daily_loss_exceeded")
        common.notify(
            f"🛑 日次損失が上限を超過（{pnl:.0f} <= -{settings.max_daily_loss_jpy:.0f}）。"
            f"Kill switch を自動 ON。",
            key="daily_loss",
        )
        _reject(sig, "daily_loss_exceeded", pnl=pnl)
        return False

    # 7.5) リスクエンジン（off=評価なし / shadow=観測ログのみ / enforce=適用）。
    #      発注レートの前に置き、enforce の却下でレート枠を消費しないようにする。
    if settings.risk_engine_mode != "off":
        allowed, engine_qty = _engine_check(sig, qty)
        if not allowed:
            return False
        if engine_qty != qty:
            sig["qty"] = engine_qty  # enforce のサイジング結果を発注数量へ反映

    # 8) 発注レート
    if not rate_limit_allow(common.r(), RATE_KEY, settings.max_orders_per_min):
        _reject(sig, "rate_limited", limit_per_min=settings.max_orders_per_min)
        return False

    common.log_event("risk_decision", {"decision": "approve", "signal": sig})
    return True


def handle(sig: dict[str, Any]) -> None:
    if evaluate(sig):
        common.publish(common.STREAM_ORDERS, sig)
        log.info("approved -> orders", **log_extra(idem=sig.get("idem"), symbol=sig.get("symbol")))


def main() -> None:
    stop = common.install_signal_handlers()
    log.info("risk service starting", **log_extra(consumer=CONSUMER))
    common.consume(
        common.STREAM_SIGNALS, GROUP, CONSUMER, handle, stop, service="risk"
    )
    log.info("risk service stopped")


if __name__ == "__main__":
    main()
