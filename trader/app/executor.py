"""③ 注文実行（IBKR / ib_async）。

Redis Consumer Group `exec` で `orders` を購読し、IB Gateway へ発注する。

ミッションクリティカル上の要点:
- 二重発注防止: idem から決定的な client_order_id を作り、発注前に
  processed_orders へ INSERT（PK 衝突なら既処理として skip）。
- Kill switch を発注直前に再確認（risk と二重チェック）。
- 本番二重ガード: trading_mode=live でも ALLOW_LIVE=1 が無ければ発注しない。
- realized_pnl: commissionReport コールバックで約定後に更新（ギャップ解消）。
- 接続耐性: 起動時リトライ＋アイドル毎に切断検知→自動再接続。

ib_async は関数内で遅延 import する（CI/テストで未接続でもモジュール import 可能）。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from typing import Any

import common
import psycopg
from config import settings
from logging_setup import log_extra, set_correlation_id, setup_logging

log = setup_logging("executor", settings.log_level)

GROUP = "exec"
CONSUMER = f"{socket.gethostname()}-{os.getpid()}"
# IBKR の異常値（PnL 非該当時の sentinel）を弾く閾値
_PNL_SENTINEL = 1e300
# strategy 由来の ATR ストップ発注の追跡（symbol -> {"ref": str, "stop_price": float}）
KEY_STOP_ORDERS = "exec:stop_orders"

_ib: Any = None  # ib_async.IB インスタンス（接続後に入る）


# ============================================================================
# 純粋ヘルパー（ib_async 不要・テスト可能）
# ============================================================================
def client_order_id(idem: str) -> str:
    """idem から決定的・短い注文参照を作る（IBKR orderRef 用）。"""
    return "tx-" + hashlib.sha1(idem.encode()).hexdigest()[:16]


def classify_symbol(symbol: str, asset: str) -> str:
    a = (asset or "").lower()
    if symbol.isdigit() or a in ("jp", "jp_stock", "jpstock", "stock_jp"):
        return "jp_stock"
    if a in ("fx", "forex", "cash", "currency"):
        return "fx"
    return "us_stock"


def stop_order_side(entry_side: str) -> str:
    """建玉を保護するストップ注文の action（エントリと反対側）を返す。"""
    return "SELL" if entry_side == "BUY" else "BUY"


def compute_stop_price(entry_side: str, fill_price: float, stop_distance: float) -> float:
    """約定価格と ATR 距離からストップ注文の発注価格(auxPrice)を計算する。"""
    if fill_price <= 0 or stop_distance <= 0:
        raise ValueError("fill_price and stop_distance must both be positive")
    price = fill_price - stop_distance if entry_side == "BUY" else fill_price + stop_distance
    return max(price, 0.01)


# ============================================================================
# IB 接続
# ============================================================================
def connect() -> None:
    """IB Gateway へ接続（指数バックオフ）。失敗は例外を上げる。"""
    global _ib
    from ib_async import IB  # 遅延 import
    from tenacity import retry, stop_after_attempt, wait_exponential

    ib = IB()

    @retry(stop=stop_after_attempt(8), wait=wait_exponential(multiplier=1, min=1, max=30))
    def _do_connect() -> None:
        ib.connect(settings.ib_host, settings.ib_port, clientId=settings.ib_client_id, timeout=15)

    _do_connect()
    ib.commissionReportEvent += _on_commission
    _ib = ib
    log.info(
        "connected to IB",
        **log_extra(host=settings.ib_host, port=settings.ib_port, mode=settings.trading_mode),
    )


def ensure_connected() -> None:
    """切断していたら再接続（アイドルフックから呼ばれる）。"""
    global _ib
    if _ib is not None and _ib.isConnected():
        _ib.sleep(0.1)  # IB のイベントループを回して接続を維持
        return
    log.warning("IB disconnected -> reconnecting")
    common.notify("⚠️ IB Gateway 切断。再接続を試みます。", key="ib_disconnect")
    try:
        connect()
    except Exception:
        log.exception("IB reconnect failed")


# ============================================================================
# 発注
# ============================================================================
def _build_contract(sig: dict[str, Any]) -> Any:
    from ib_async import Forex, Stock

    symbol = sig["symbol"]
    kind = classify_symbol(symbol, sig.get("asset", ""))
    if kind == "fx":
        return Forex(symbol)
    if kind == "jp_stock":
        return Stock(symbol, "TSEJ", "JPY")
    return Stock(symbol, "SMART", "USD")


def _build_order(sig: dict[str, Any], order_ref: str) -> Any:
    from ib_async import LimitOrder, MarketOrder

    action = sig["side"]  # BUY / SELL
    qty = float(sig["qty"])
    if sig.get("type") == "LIMIT" and sig.get("price"):
        order = LimitOrder(action, qty, float(sig["price"]))
    else:
        order = MarketOrder(action, qty)
    order.orderRef = order_ref
    return order


def _claim(idem: str, coid: str) -> bool:
    """発注権を確保。新規なら True、既処理なら False。"""
    try:
        common.db_execute(
            "INSERT INTO processed_orders (idem, client_order_id) VALUES (%s, %s)",
            (idem, coid),
        )
        return True
    except psycopg.errors.UniqueViolation:
        return False


def _prior_status(idem: str) -> str | None:
    rows = common.db_query("SELECT status FROM processed_orders WHERE idem = %s", (idem,))
    return rows[0][0] if rows else None


def handle(sig: dict[str, Any]) -> None:
    idem = sig.get("idem", "")
    set_correlation_id(idem)
    coid = client_order_id(idem)

    # 発注直前の Kill switch 再確認（二重チェック）
    if common.kill_switch_on():
        log.warning("kill switch ON at executor -> skip", **log_extra(idem=idem))
        return

    # 本番二重ガード
    if settings.trading_mode == "live" and not settings.allow_live:
        common.notify("⛔ live モードだが ALLOW_LIVE=0 のため発注しない。", key="live_guard")
        log.error("live guard blocked order", **log_extra(idem=idem))
        return

    # 冪等: 発注権の確保
    if not _claim(idem, coid):
        status = _prior_status(idem)
        if status == "submitting":
            # 過去に確保したが完了記録が無い（クラッシュ等）。重複発注を避け、点検に回す。
            common.notify(
                f"⚠️ 未完了の発注記録あり idem={idem} status={status}。"
                f"reconcile で要確認（重複回避のため再発注しません）。",
                key=f"stale_order:{idem}",
            )
            log.error("stale processed_order -> manual reconcile", **log_extra(idem=idem))
        else:
            log.info("already processed -> skip", **log_extra(idem=idem, status=status))
        return

    # 発注
    contract = _build_contract(sig)
    order = _build_order(sig, coid)
    try:
        trade = _ib.placeOrder(contract, order)
        _ib.sleep(1.0)  # 状態更新を待つ
        status = trade.orderStatus.status or "Submitted"
        ref = str(getattr(trade.order, "orderRef", coid))
        common.db_execute(
            "UPDATE processed_orders SET status = %s, broker_ref = %s WHERE idem = %s",
            ("submitted", ref, idem),
        )
        _record_fill(sig, status=status, ref=ref)
        common.log_event("order_submitted", {"signal": sig, "status": status, "ref": ref})
        common.notify(
            f"✅ 発注 {sig['side']} {sig['symbol']} x{sig['qty']:g} "
            f"({settings.trading_mode}/{status})",
            key=f"order_ok:{idem}",
            throttle=False,
        )
        log.info("order submitted", **log_extra(idem=idem, status=status, ref=ref))

        # strategy 由来の ATR ストップを実発注（webhook 等 stop_distance の無い signal は対象外）。
        # 失敗してもエントリ自体は成功しているので、ここでは raise せず通知のみに留める。
        if sig.get("stop_distance"):
            try:
                _place_protective_stop(sig, contract, coid, trade)
            except Exception:
                log.exception("protective stop handling failed", **log_extra(idem=idem))
                common.notify(
                    f"⚠️ ATRストップ発注に失敗 symbol={sig.get('symbol')}。手動で確認してください。",
                    key=f"stop_err:{sig.get('symbol')}",
                    throttle=False,
                )
    except Exception as e:
        common.db_execute(
            "UPDATE processed_orders SET status = %s WHERE idem = %s", ("error", idem)
        )
        common.log_event("order_error", {"signal": sig, "error": str(e)})
        common.notify(
            f"❌ 発注失敗 {sig.get('side')} {sig.get('symbol')}: {e}",
            key=f"order_err:{idem}",
            throttle=False,
        )
        log.exception("order failed", **log_extra(idem=idem))
        _bump_error_counter()
        raise  # consume 側でリトライ/最終的に dead-letter


def _record_fill(sig: dict[str, Any], *, status: str, ref: str) -> None:
    common.db_execute(
        "INSERT INTO fills (ts, symbol, side, qty, status, broker, ref, realized_pnl, idem) "
        "VALUES (now(), %s, %s, %s, %s, %s, %s, 0, %s)",
        (sig["symbol"], sig["side"], float(sig["qty"]), status, "IBKR", ref, sig.get("idem")),
    )
    # 発注が通ったので連続エラーカウンタをリセット
    try:
        common.r().set(common.KEY_CONSEC_ERRORS, 0)
    except Exception:
        pass


def _cancel_tracked_stop(symbol: str) -> None:
    """直前に張った保護ストップを取消（反転/再エントリで古いストップが残らないように）。

    既に約定済み（ストップ到達でクローズ済み）の場合は openTrades() に現れないため無害。
    """
    raw = common.r().hget(KEY_STOP_ORDERS, symbol)
    common.r().hdel(KEY_STOP_ORDERS, symbol)
    if not raw:
        return
    try:
        info = json.loads(raw)
    except Exception:
        return
    try:
        for t in _ib.openTrades():
            if str(getattr(t.order, "orderRef", "")) == info.get("ref"):
                _ib.cancelOrder(t.order)
                log.info("cancelled stale protective stop", **log_extra(symbol=symbol, ref=info.get("ref")))
    except Exception:
        log.exception("failed to cancel existing protective stop", **log_extra(symbol=symbol))


def _place_protective_stop(sig: dict[str, Any], contract: Any, coid: str, trade: Any) -> None:
    """エントリ約定後に ATR ストップ（STP 注文）を実発注し、追跡情報を Redis に記録する。"""
    from ib_async import StopOrder

    stop_distance = float(sig.get("stop_distance") or 0)
    if stop_distance <= 0:
        return
    fill_price = float(getattr(trade.orderStatus, "avgFillPrice", 0) or 0)
    if fill_price <= 0:
        log.error("protective stop skipped: no fill price", **log_extra(idem=sig.get("idem")))
        common.notify(
            f"⚠️ ATRストップ未設定（約定価格が取得できません）symbol={sig.get('symbol')}",
            key=f"stop_missing:{sig.get('symbol')}",
            throttle=False,
        )
        return

    symbol = sig["symbol"]
    _cancel_tracked_stop(symbol)

    side = stop_order_side(sig["side"])
    # position_qty は反転後の想定建玉サイズ（発注の qty はフリップ分を含むため使わない）
    qty = float(sig.get("position_qty") or sig["qty"])
    stop_price = compute_stop_price(sig["side"], fill_price, stop_distance)
    ref = coid + "-stop"

    order = StopOrder(side, qty, round(stop_price, 5))
    order.orderRef = ref
    order.tif = "GTC"
    _ib.placeOrder(contract, order)
    _ib.sleep(0.3)
    common.r().hset(KEY_STOP_ORDERS, symbol, json.dumps({"ref": ref, "stop_price": stop_price}))
    common.log_event(
        "stop_order_submitted",
        {"symbol": symbol, "side": side, "qty": qty, "stop_price": stop_price, "ref": ref},
    )
    log.info(
        "protective stop placed",
        **log_extra(symbol=symbol, side=side, qty=qty, stop_price=stop_price, ref=ref),
    )


def _bump_error_counter() -> None:
    """連続エラーが閾値に達したら自動 Kill switch。"""
    try:
        n = common.r().incr(common.KEY_CONSEC_ERRORS)
    except Exception:
        return
    if n >= settings.max_consecutive_errors:
        common.set_kill_switch(True, reason="consecutive_errors")
        common.notify(
            f"🛑 連続発注エラー {n} 回。Kill switch を自動 ON。", key="consecutive_errors"
        )


def _on_commission(trade: Any, _fill: Any, report: Any) -> None:
    """約定後の commissionReport から realized_pnl を更新（ギャップ解消）。"""
    pnl = getattr(report, "realizedPNL", None)
    if pnl is None or abs(pnl) >= _PNL_SENTINEL:
        return
    ref = str(getattr(trade.order, "orderRef", "") or "")
    if not ref:
        return
    try:
        common.db_execute("UPDATE fills SET realized_pnl = %s WHERE ref = %s", (float(pnl), ref))
        log.info("realized pnl updated", **log_extra(ref=ref, pnl=pnl))
    except Exception:
        log.exception("failed to update realized pnl", **log_extra(ref=ref))


# ============================================================================
# main
# ============================================================================
def main() -> None:
    stop = common.install_signal_handlers()
    log.info("executor starting", **log_extra(consumer=CONSUMER, mode=settings.trading_mode))
    connect()
    # 起動時リコンサイル（前回クラッシュの取りこぼし/未完了を検知）
    try:
        import reconcile

        reconcile.run_once(_ib)
    except Exception:
        log.exception("startup reconcile failed (continuing)")
    try:
        common.consume(
            common.STREAM_ORDERS,
            GROUP,
            CONSUMER,
            handle,
            stop,
            service="executor",
            block_ms=1000,
            on_idle=ensure_connected,
        )
    finally:
        if _ib is not None and _ib.isConnected():
            _ib.disconnect()
    log.info("executor stopped")


if __name__ == "__main__":
    main()
