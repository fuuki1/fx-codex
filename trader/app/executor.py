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
# 銘柄ごとの「現在保有ポジションのエントリー時リスク額」。R 倍数（realized_pnl ÷ リスク）の
# 分母に使う。決済 fill 側の intended_risk（別ポジション/0 になりうる）で割ると誤るため、
# エントリーのリスクを覚えておき、決済で実現損益が出たときにこれで割る。
KEY_ENTRY_RISK = "risk:entry_risk"
# 発注ごとの文脈（キー `exec:order_ctx:<orderRef>` -> {idem/intended_risk/stop_distance/...}）。
# 実約定は execDetailsEvent で非同期に届くので、その時に fills へ載せる付帯情報をここから引く。
# キー単位の TTL で自動失効させる（発注ログは events / processed_orders が保持する）。
KEY_ORDER_CTX = "exec:order_ctx"
ORDER_CTX_TTL_SEC = 7 * 24 * 3600
# 保護ストップ注文の参照は親 orderRef にこの接尾辞を付ける（撤退時の取消・突合で識別する）。
STOP_REF_SUFFIX = ":stop"

_ib: Any = None  # ib_async.IB インスタンス（接続後に入る）


# ============================================================================
# R 倍数（entry リスク基準）— 純粋 + Redis ヘルパー（テスト可能）
# ============================================================================
def realized_r_multiple(pnl: float, entry_risk: float | None) -> float | None:
    """実現損益 ÷ エントリー時リスク額。リスク不明/非正なら None。"""
    if entry_risk is None or entry_risk <= 0:
        return None
    return pnl / entry_risk


def record_entry_risk(symbol: str, intended_risk: float) -> None:
    """新規建て時のリスク額を銘柄ごとに記録（既に保有中なら上書きしない）。

    hsetnx で「最初のエントリー」のリスクだけを残す（決済注文のリスクで上書きしない）。
    intended_risk<=0（サイジング無し等）は記録しない → R 倍数は算出不能(NULL)扱い。
    """
    if intended_risk <= 0:
        return
    try:
        common.r().hsetnx(KEY_ENTRY_RISK, symbol, intended_risk)
    except Exception:
        log.exception("failed to record entry risk", **log_extra(symbol=symbol))


def pop_entry_risk(symbol: str) -> float | None:
    """保有ポジションのエントリー時リスクを取り出して消す（決済＝ポジション解消時）。"""
    try:
        v = common.r().hget(KEY_ENTRY_RISK, symbol)
        common.r().hdel(KEY_ENTRY_RISK, symbol)
        return float(v) if v is not None else None
    except Exception:
        log.exception("failed to pop entry risk", **log_extra(symbol=symbol))
        return None


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


# ============================================================================
# 保護ストップ / 撤退 / 約定 — 純粋ヘルパー（ib_async 不要・テスト可能）
# ============================================================================
_EXIT_INTENTS = {"exit", "close", "flat"}


def reverse_action(side: str) -> str:
    """反対売買のサイド（BUY<->SELL）。保護ストップ・決済で使う。"""
    return "SELL" if side.upper() == "BUY" else "BUY"


def is_exit_order(sig: dict[str, Any]) -> bool:
    """このシグナルが「撤退（フラット化）」かどうか。strategy が intent=exit を載せる。"""
    return str(sig.get("intent", "")).lower() in _EXIT_INTENTS


def wants_protective_stop(sig: dict[str, Any]) -> bool:
    """このエントリーに保護ストップを付けるべきか（撤退でなく、正の stop_distance を持つ）。"""
    if is_exit_order(sig):
        return False
    sd = sig.get("stop_distance")
    try:
        return sd not in (None, "") and float(sd) > 0
    except (TypeError, ValueError):
        return False


def protective_stop_price(entry_side: str, ref_price: float, stop_distance: float) -> float:
    """エントリー価格から保護ストップ価格を出す（バックテストの ATR ストップと対応）。

    ロング（BUY エントリー）は下側 ref-dist、ショート（SELL エントリー）は上側 ref+dist。
    """
    return ref_price - stop_distance if entry_side.upper() == "BUY" else ref_price + stop_distance


def exec_side_to_action(exec_side: str) -> str:
    """IBKR の約定サイド（BOT/SLD）を内部サイド（BUY/SELL）へ。"""
    return "BUY" if str(exec_side).upper().startswith("B") else "SELL"


def symbol_matches_contract(contract: Any, symbol: str) -> bool:
    """IB コントラクトが対象シンボルか（FX の localSymbol/通貨結合を正規化して照合）。"""
    want = symbol.upper().replace(".", "").replace("/", "")
    sym = (getattr(contract, "symbol", "") or "").upper()
    cur = (getattr(contract, "currency", "") or "").upper()
    local = (getattr(contract, "localSymbol", "") or "").upper().replace(".", "").replace("/", "")
    return want in {sym, sym + cur, local}


def parse_execution(fill: Any) -> dict[str, Any] | None:
    """ib_async の Fill から約定 1 件を辞書へ（execId が無ければ None）。"""
    execu = getattr(fill, "execution", None)
    if execu is None:
        return None
    exec_id = getattr(execu, "execId", "") or ""
    if not exec_id:
        return None
    contract = getattr(fill, "contract", None)
    return {
        "exec_id": exec_id,
        "side": exec_side_to_action(getattr(execu, "side", "")),
        "shares": float(getattr(execu, "shares", 0) or 0),
        "price": float(getattr(execu, "price", 0) or 0),
        "ref": str(getattr(execu, "orderRef", "") or ""),
        "symbol": (getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""),
    }


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
    # 実約定を fills へ記録（execDetails）＋ 決済損益を反映（commissionReport）。
    ib.execDetailsEvent += _on_exec_details
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


# ---- 発注文脈（execDetails で fills に載せる付帯情報を orderRef で引く）--------
def _ctx_key(ref: str) -> str:
    return f"{KEY_ORDER_CTX}:{ref}"


def save_order_ctx(ref: str, ctx: dict[str, Any]) -> None:
    try:
        common.r().set(_ctx_key(ref), json.dumps(ctx, default=str), ex=ORDER_CTX_TTL_SEC)
    except Exception:
        log.exception("failed to save order ctx", **log_extra(ref=ref))


def load_order_ctx(ref: str) -> dict[str, Any]:
    try:
        raw = common.r().get(_ctx_key(ref))
        return json.loads(raw) if raw else {}
    except Exception:
        log.exception("failed to load order ctx", **log_extra(ref=ref))
        return {}


def _reference_price(trade: Any, _sig: dict[str, Any]) -> float | None:
    """保護ストップの基準価格 = 実際の平均約定価格（未約定なら None）。

    未約定の指値価格を基準にストップを置くと、約定しなかった時に「建玉が無いのに生きた
    ストップ」＝意図しない建玉を招く。基準は必ず **実約定平均** を使い、取れなければ
    ストップを置かず警告する（成行は 1 秒待機後にはほぼ約定済みでこの値が入る）。
    """
    avg = float(getattr(trade.orderStatus, "avgFillPrice", 0) or 0)
    return avg if avg > 0 else None


def _place_protective_stop(contract: Any, sig: dict[str, Any], parent_ref: str, ref_price: float) -> None:
    """エントリー約定に対して保護ストップ（IBKR STP）を出す。

    バックテストの ATR ストップ（保有中に高値/安値でストップ）に対応するライブ側の実装。
    ストップ参照は ``<parent_ref>:stop`` とし、撤退時の取消・reconcile 突合で識別できるようにする。
    """
    from ib_async import StopOrder

    entry_side = sig["side"]
    qty = float(sig["qty"])
    stop_distance = float(sig["stop_distance"])
    stop_price = protective_stop_price(entry_side, ref_price, stop_distance)
    stop_ref = parent_ref + STOP_REF_SUFFIX
    order = StopOrder(reverse_action(entry_side), qty, round(stop_price, 5))
    order.orderRef = stop_ref
    _ib.placeOrder(contract, order)
    # ストップ約定（= 決済）も fills に実約定として載るよう、文脈を保存しておく。
    save_order_ctx(stop_ref, {
        "symbol": sig["symbol"], "idem": sig.get("idem"), "asset": sig.get("asset"),
        "intended_risk": 0.0, "stop_distance": None, "intent": "stop",
    })
    common.log_event("protective_stop", {
        "symbol": sig["symbol"], "ref": stop_ref, "stop_price": stop_price,
        "entry_side": entry_side, "qty": qty,
    })
    log.info("protective stop placed",
             **log_extra(symbol=sig["symbol"], ref=stop_ref, stop_price=stop_price))


def _cancel_protective_stops(symbol: str) -> int:
    """対象シンボルの未約定な保護ストップを取り消す（撤退でフラット化する前に呼ぶ）。

    未約定ストップを残すと、撤退後にストップが生き残って反対建てしてしまう。``:stop`` 参照で
    識別し、ブローカーの未約定注文から該当を取り消す。取り消した件数を返す。
    """
    cancelled = 0
    try:
        for trade in _ib.openTrades():
            ref = str(getattr(trade.order, "orderRef", "") or "")
            if ref.endswith(STOP_REF_SUFFIX) and symbol_matches_contract(trade.contract, symbol):
                _ib.cancelOrder(trade.order)
                cancelled += 1
    except Exception:
        log.exception("failed to cancel protective stops", **log_extra(symbol=symbol))
    if cancelled:
        log.info("protective stops cancelled", **log_extra(symbol=symbol, count=cancelled))
    return cancelled


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

    # 撤退（フラット化）は先に保護ストップを取り消す（残すと決済後に反対建てしてしまう）。
    if is_exit_order(sig):
        _cancel_protective_stops(sig["symbol"])

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
        # 実約定は execDetailsEvent が fills へ記録する（本 handle では fills に触れない）。
        # execDetails が付帯情報（idem/リスク/ストップ距離）を載せられるよう文脈を保存する。
        save_order_ctx(ref, {
            "symbol": sig["symbol"], "idem": sig.get("idem"), "asset": sig.get("asset"),
            "intended_risk": float(sig.get("intended_risk") or 0.0),
            "stop_distance": sig.get("stop_distance"),
            "intent": sig.get("intent", "entry"),
        })
        # このポジションのエントリー時リスクを覚えておく（決済時に R 倍数の分母として使う）。
        record_entry_risk(sig["symbol"], float(sig.get("intended_risk") or 0.0))
        # 保護ストップ（バックテストの ATR ストップに対応）を出す。
        if wants_protective_stop(sig):
            ref_price = _reference_price(trade, sig)
            if ref_price is not None:
                _place_protective_stop(contract, sig, ref, ref_price)
            else:
                common.notify(
                    f"⚠️ 保護ストップ未設置（基準価格不明）{sig['symbol']} idem={idem}。要確認。",
                    key=f"no_stop:{idem}",
                )
                log.error("could not place protective stop: no reference price",
                          **log_extra(idem=idem, symbol=sig["symbol"]))
        _reset_error_counter()
        common.log_event("order_submitted", {"signal": sig, "status": status, "ref": ref})
        common.notify(
            f"✅ 発注 {sig['side']} {sig['symbol']} x{sig['qty']:g} "
            f"({settings.trading_mode}/{status})",
            key=f"order_ok:{idem}",
            throttle=False,
        )
        log.info("order submitted", **log_extra(idem=idem, status=status, ref=ref))
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


def _reset_error_counter() -> None:
    """発注が通ったので連続エラーカウンタをリセット（best-effort）。"""
    try:
        common.r().set(common.KEY_CONSEC_ERRORS, 0)
    except Exception:
        pass


def record_execution(execu: dict[str, Any], ctx: dict[str, Any]) -> None:
    """実約定 1 件を fills へ記録する（execId で冪等）。

    旧実装は「発注直後の想定行（想定数量・Submitted 状態・realized_pnl=0）」を fills へ
    書いており、**実際の約定履歴になっていなかった**（未約定・部分約定・約定価格が反映されない）。
    本関数は IBKR の execDetails（実約定）を、約定価格・約定数量・execId 付きで記録する。
    realized_pnl は後続の commissionReport が execId 基準で更新する。
    """
    intended_risk = float(ctx.get("intended_risk") or 0.0)
    sd = ctx.get("stop_distance")
    stop_distance = float(sd) if sd not in (None, "") else None
    # execId で冪等化する（execDetails は再接続で再送されうる）。fills は TimescaleDB の
    # hypertable のため、一意索引に区分キー ts を含める必要があり ON CONFLICT(exec_id) が
    # 使えない。executor は単一コンシューマ＋IB イベントループ直列なので、存在チェック→
    # INSERT で十分に安全（重複は稀な再送のみ）。
    if common.db_query("SELECT 1 FROM fills WHERE exec_id = %s LIMIT 1", (execu["exec_id"],)):
        return
    # 正規化済みのペア表記（例 USDJPY）を優先して保存する。ブローカーの localSymbol（USD.JPY）
    # のままだと record_entry_risk（正規化シンボルで保持）や risk のペア分解と食い違うため。
    symbol = ctx.get("symbol") or execu["symbol"] or ""
    common.db_execute(
        "INSERT INTO fills "
        "(ts, symbol, side, qty, status, broker, ref, realized_pnl, idem, "
        " intended_risk, stop_distance, fill_price, exec_id) "
        "VALUES (now(), %s, %s, %s, 'filled', 'IBKR', %s, 0, %s, %s, %s, %s, %s)",
        (symbol, execu["side"], execu["shares"], execu["ref"],
         ctx.get("idem"), intended_risk, stop_distance, execu["price"], execu["exec_id"]),
    )


def _on_exec_details(trade: Any, fill: Any) -> None:
    """IBKR execDetails（実約定）を fills へ記録する。"""
    execu = parse_execution(fill)
    if execu is None:
        return
    ref = execu["ref"] or str(getattr(trade.order, "orderRef", "") or "")
    execu["ref"] = ref
    ctx = load_order_ctx(ref)
    try:
        record_execution(execu, ctx)
        log.info("execution recorded",
                 **log_extra(exec_id=execu["exec_id"], symbol=execu["symbol"],
                             side=execu["side"], qty=execu["shares"], price=execu["price"]))
    except Exception:
        log.exception("failed to record execution", **log_extra(exec_id=execu["exec_id"]))


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


def _on_commission(trade: Any, fill: Any, report: Any) -> None:
    """commissionReport から該当約定（execId）の realized_pnl / R 倍数を更新する。

    実約定行（record_execution が execId で作成）にひも付けて更新するため、複数約定や
    エントリー/決済が混在しても正しい行だけを更新できる（旧実装は orderRef 一致で全件を
    まとめて更新していた）。
    """
    pnl = getattr(report, "realizedPNL", None)
    if pnl is None or abs(pnl) >= _PNL_SENTINEL:
        return
    exec_id = str(getattr(report, "execId", "") or getattr(getattr(fill, "execution", None), "execId", "") or "")
    if not exec_id:
        return
    try:
        # realized_pnl は決済約定に計上される（=往復損益）。R 倍数はこの決済注文の
        # intended_risk ではなく「エントリー時のリスク」で割る必要がある。約定行の
        # シンボルを取り、そのポジションのエントリーリスクを引いて割る。
        rows = common.db_query("SELECT symbol FROM fills WHERE exec_id = %s LIMIT 1", (exec_id,))
        symbol = rows[0][0] if rows else None
        # 実現損益が出た（決済）約定のみエントリーリスクを消費して R 倍数を出す。
        entry_risk = pop_entry_risk(symbol) if (symbol and float(pnl) != 0.0) else None
        r_mult = realized_r_multiple(float(pnl), entry_risk)
        common.db_execute(
            "UPDATE fills SET realized_pnl = %s, realized_r = %s WHERE exec_id = %s",
            (float(pnl), r_mult, exec_id),
        )
        log.info("realized pnl updated", **log_extra(exec_id=exec_id, pnl=pnl, r=r_mult))
    except Exception:
        log.exception("failed to update realized pnl", **log_extra(exec_id=exec_id))


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
