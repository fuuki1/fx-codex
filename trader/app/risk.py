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
  8. 発注レート（MAX_ORDERS_PER_MIN, Redis 永続のスライディングウィンドウ）
"""
from __future__ import annotations

import os
import socket
from typing import Any

import common
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
