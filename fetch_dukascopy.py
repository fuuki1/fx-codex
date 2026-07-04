#!/usr/bin/env python3
"""Dukascopy 実ティックデータを取得し、fx_backtester が読めるOHLC CSVへ変換する。

レポート(FX AI.md)第0段階「Dukascopyティック＋実ブローカーフィード」の入口。
無料の Dukascopy データフィードから .bi5(LZMA圧縮ティック)を1時間ずつ取得し、
指定した時間足のOHLCバーへ集約して、そのまま `fx_backtester --data` に渡せる
CSV(timestamp,symbol,open,high,low,close,volume,spread_price)を書き出す。

追加のサードパーティ依存は無い(標準ライブラリ + requests のみ)。生 .bi5 は
--cache-dir 以下へ保存され、二度目以降はネットワークに触れない(Dukascopy の
IP制限対策)。

使い方:
    # USDJPY の1時間足を1週間ぶん取得して CSV 化
    python3 fetch_dukascopy.py --symbol USDJPY \\
        --start 2025-06-01 --end 2025-06-08 --timeframe 1h \\
        --out runs/data/USDJPY_1h.csv

    # 複数ペアをまとめて(各ペア別ファイル)
    python3 fetch_dukascopy.py --symbol USDJPY EURUSD GBPUSD \\
        --start 2025-06-01 --end 2025-06-02 --timeframe 15m --out-dir runs/data

出力CSVは fx_backtester のデータ品質ゲート(OHLC整合性・spread>0)を通る形式で、
spread_price に Dukascopy の bid/ask 実スプレッドが入るため、バックテストの
約定コストを合成値でなく実測値でモデル化できる。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

from fx_intel.dukascopy import TIMEFRAME_MINUTES, download_bars_csv


def _parse_date(value: str) -> datetime:
    """YYYY-MM-DD または YYYY-MM-DD HH:MM を UTC の datetime に読む。"""
    raw = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"日付として解釈できません: {value!r}(例: 2025-06-01)")


def _default_out_path(out_dir: Path, symbol: str, timeframe: str) -> Path:
    return out_dir / f"{symbol.upper()}_{timeframe}.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dukascopy ティックを取得して fx_backtester 用 OHLC CSV を作る",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbol", nargs="+", required=True, help="通貨ペア(例: USDJPY EURUSD)")
    parser.add_argument("--start", type=_parse_date, required=True, help="開始日 YYYY-MM-DD(UTC)")
    parser.add_argument(
        "--end",
        type=_parse_date,
        default=None,
        help="終了日 YYYY-MM-DD(UTC、含む)。省略時は開始日の翌日まで。",
    )
    parser.add_argument(
        "--timeframe",
        default="1h",
        choices=sorted(TIMEFRAME_MINUTES, key=lambda tf: TIMEFRAME_MINUTES[tf]),
        help="集約する時間足(既定: 1h)",
    )
    parser.add_argument("--out", default=None, help="出力CSVパス(単一ペア時)")
    parser.add_argument(
        "--out-dir",
        default="runs/data",
        help="複数ペア時の出力先ディレクトリ(既定: runs/data)。--out 未指定時にも使用。",
    )
    parser.add_argument(
        "--cache-dir",
        default="runs/dukascopy_cache",
        help="生 .bi5 のキャッシュ先(既定: runs/dukascopy_cache)",
    )
    args = parser.parse_args(argv)

    end = args.end or (args.start + timedelta(days=1))
    if end < args.start:
        parser.error("--end は --start 以降にしてください")

    if args.out is not None and len(args.symbol) > 1:
        parser.error("--out は単一ペア専用です。複数ペアは --out-dir を使ってください。")

    out_dir = Path(args.out_dir)
    exit_code = 0
    for symbol in args.symbol:
        out_path = (
            Path(args.out)
            if args.out is not None
            else _default_out_path(out_dir, symbol, args.timeframe)
        )
        print(
            f"[{symbol}] {args.start:%Y-%m-%d %H:%M} 〜 {end:%Y-%m-%d %H:%M} UTC "
            f"を {args.timeframe} 足で取得中…",
            file=sys.stderr,
        )
        result = download_bars_csv(
            symbol=symbol,
            start=args.start,
            end=end,
            timeframe=args.timeframe,
            out_path=out_path,
            cache_dir=args.cache_dir,
        )
        for warning in result.warnings:
            print(f"  ⚠️ {warning}", file=sys.stderr)
        if result.out_path is None:
            print(f"  ✗ {symbol}: CSV未出力(ティック{result.tick_count}件)", file=sys.stderr)
            exit_code = 1
            continue
        print(
            f"  ✓ {result.out_path} — {result.bar_count}本 "
            f"(ティック{result.tick_count}件)",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
