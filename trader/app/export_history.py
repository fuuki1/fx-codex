"""IB Gateway から日足ヒストリカルデータを取得し、fx_backtester 互換 CSV を書き出す。

`optimize/auto_optimize.py`（ホスト側の自律最適化エンジン）が、最適化対象を同梱サンプルの
固定データではなく実際の市場データにするために `docker compose run` 経由で呼び出す。
strategy.py / reconcile.py と同じ接続パターン（別 clientId で IB Gateway へ接続）。

  usage: python export_history.py --out /path/to/history.csv [--symbol USDJPY] [--asset fx] [--years 5]

失敗（IB 未接続・データ無し等）は非ゼロ終了し、既存ファイルは変更しない
（auto_optimize.py 側で同梱サンプルへフォールバックする）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from config import settings
from logging_setup import setup_logging

log = setup_logging("export_history", settings.log_level)

# executor(=ib_client_id) / strategy(+70) / reconcile(+50) と衝突しないオフセット
CLIENT_ID_OFFSET = 90


def bars_to_csv_rows(bars: list[Any]) -> list[dict[str, Any]]:
    """ib_async の BarData 相当のオブジェクト列を CSV 行（dict）に変換する（純粋関数）。"""
    return [
        {
            "timestamp": str(getattr(b, "date", "")),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
        }
        for b in bars
    ]


def write_csv(rows: list[dict[str, Any]], out_path: Path) -> None:
    """CSV を書き出す。空データで既存ファイルを壊さないよう、行が無ければ例外を投げる。"""
    import pandas as pd

    if not rows:
        raise ValueError("no bars fetched; refusing to write an empty history file")
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(out_path)  # アトミックに差し替え（読み手が半端な内容を見ない）


def fetch_daily_bars(ib: Any, symbol: str, asset: str, years: int) -> list[Any]:
    contract: Any
    if asset.lower() in ("fx", "forex", "cash", "currency"):
        from ib_async import Forex

        contract = Forex(symbol)
    else:
        from ib_async import Stock

        contract = Stock(symbol, "SMART", "USD")
    return ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=f"{max(int(years), 1)} Y",
        barSizeSetting="1 day",
        whatToShow="MIDPOINT",
        useRTH=False,
        formatDate=1,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--symbol", default=settings.strategy_symbol)
    parser.add_argument("--asset", default=settings.strategy_asset)
    parser.add_argument("--years", type=int, default=5)
    args = parser.parse_args(argv)

    from ib_async import IB

    ib = IB()
    try:
        ib.connect(
            settings.ib_host, settings.ib_port, clientId=settings.ib_client_id + CLIENT_ID_OFFSET, timeout=20
        )
    except Exception:
        log.exception("export_history could not connect to IB")
        return 1

    try:
        bars = fetch_daily_bars(ib, args.symbol, args.asset, args.years)
        rows = bars_to_csv_rows(bars)
        write_csv(rows, Path(args.out))
        log.info("history exported", extra={"extra_fields": {"bars": len(rows), "out": args.out}})
        print(f"wrote {len(rows)} bars -> {args.out}")
        return 0
    except Exception:
        log.exception("export_history failed")
        return 1
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
