"""リコンサイル: ブローカーの実状態と DB を突合し、差異を通知する。

呼び出し方:
- executor 起動時に run_once(ib) を 1 回（既存接続を再利用）
- 手動 / 定期: `python reconcile.py --once` または `--loop --interval 300`

完全自動の是正はしない（誤是正のほうが危険）。差異は必ず人に上げる方針。
"""
from __future__ import annotations

import argparse
from typing import Any

import common
from config import settings
from logging_setup import log_extra, setup_logging

log = setup_logging("reconcile", settings.log_level)

STALE_THRESHOLD_SEC = 300


def stale_submitting(threshold_sec: int = STALE_THRESHOLD_SEC) -> list[str]:
    """発注確保したまま完了記録が無い（クラッシュ疑い）idem 一覧。"""
    rows = common.db_query(
        "SELECT idem FROM processed_orders "
        "WHERE status = 'submitting' AND submitted_at < now() - make_interval(secs => %s)",
        (threshold_sec,),
    )
    return [r[0] for r in rows]


def _broker_state(ib: Any) -> dict[str, Any]:
    positions = []
    open_refs = []
    try:
        for p in ib.positions():
            positions.append(
                {"symbol": getattr(p.contract, "localSymbol", "") or p.contract.symbol,
                 "position": p.position, "avgCost": p.avgCost}
            )
        ib.sleep(0.2)
        for t in ib.reqAllOpenOrders():
            open_refs.append(str(getattr(t.order, "orderRef", "") or ""))
    except Exception:
        log.exception("failed to read broker state")
    return {"positions": positions, "open_order_refs": open_refs}


def run_once(ib: Any | None = None) -> dict[str, Any]:
    discrepancies: list[str] = []

    stale = stale_submitting()
    if stale:
        discrepancies.append(f"未完了の発注記録 {len(stale)} 件: {stale[:5]}")

    broker: dict[str, Any] = {}
    if ib is not None:
        broker = _broker_state(ib)
        known = {
            r[0]
            for r in common.db_query("SELECT client_order_id FROM processed_orders")
        }
        orphans = [ref for ref in broker.get("open_order_refs", []) if ref and ref not in known]
        if orphans:
            discrepancies.append(f"DBに無いブローカー未約定注文: {orphans[:5]}")

    result = {"discrepancies": discrepancies, "broker": broker, "stale": stale}
    common.log_event("reconcile", result)

    if discrepancies:
        common.notify("🔎 リコンサイル差異:\n- " + "\n- ".join(discrepancies), key="reconcile_diff")
        log.warning("reconcile found discrepancies", **log_extra(count=len(discrepancies)))
    else:
        log.info("reconcile clean", **log_extra(positions=len(broker.get("positions", []))))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="定期実行")
    parser.add_argument("--once", action="store_true", help="1 回だけ実行（既定）")
    parser.add_argument("--interval", type=int, default=300, help="--loop 時の間隔（秒）")
    args = parser.parse_args()

    ib = None
    try:
        from ib_async import IB

        ib = IB()
        # executor と別 clientId で接続（衝突回避）
        ib.connect(settings.ib_host, settings.ib_port, clientId=settings.ib_client_id + 50, timeout=15)
    except Exception:
        log.exception("reconcile could not connect to IB (continuing DB-only checks)")
        ib = None

    stop = common.install_signal_handlers()
    try:
        if args.loop:
            while not stop.is_set():
                run_once(ib)
                stop.wait(args.interval)
        else:
            run_once(ib)
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    main()
