"""② リスク管理・フィルタ。

Redis Consumer Group `risk` で `signals` を購読し、全チェック通過分だけ
`orders` へ流す。チェックは上から順に、1 つでも引っかかれば却下。

  1. Kill switch
  2. 数量上限（MAX_POSITION_QTY）
  3. 取引時間帯（within_session, ENFORCE_SESSION のとき）
  4. 日次損失（超過で自動 Kill switch ON + 通知）
  5. 発注レート（MAX_ORDERS_PER_MIN, Redis 永続のスライディングウィンドウ）
"""
from __future__ import annotations

import os
import socket
from typing import Any

import common
import holidays
from config import settings
from domain import rate_limit_allow, within_session
from logging_setup import log_extra, set_correlation_id, setup_logging

log = setup_logging("risk", settings.log_level)

GROUP = "risk"
CONSUMER = f"{socket.gethostname()}-{os.getpid()}"
RATE_KEY = "rate:orders"


def _today_realized_pnl() -> float:
    rows = common.db_query(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM fills WHERE ts >= date_trunc('day', now())"
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

    # 2) 数量上限
    qty = float(sig.get("qty", 0))
    if qty > settings.max_position_qty:
        _reject(sig, "qty_over_limit", qty=qty, limit=settings.max_position_qty)
        return False

    # 3) 取引時間帯（休日カレンダーは holidays.get_calendar() が I/O 側でホットリロード）
    if settings.enforce_session and not within_session(
        sig.get("asset", ""), sig.get("symbol", ""), holidays=holidays.get_calendar()
    ):
        _reject(sig, "out_of_session", asset=sig.get("asset"), symbol=sig.get("symbol"))
        return False

    # 4) 日次損失
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

    # 5) 発注レート
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
