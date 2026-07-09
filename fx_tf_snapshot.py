"""時間足別採点用の価格スナップショットを5分ごとに記録する軽量スクリプト。

時間足別モード(fx_briefing.py --per-timeframe)の自己採点は、各判断を
「記録時刻 + その足の主ホライズン」時点の実勢価格と突き合わせる。将来価格は
ジャーナルの後続エントリ(源A)から取るが、判断ジャーナルは毎時しか追記されない
ため、短い足(特に 15m: 主ホライズン15分)は採点窓[9,21分]に入る点が
永久に得られず、学習が回らない。

このスクリプトは判断・Discord通知とは切り離し、TradingView から各時間足の
現在価格スナップショットを5分ごとに取得して専用の価格系列
logs/briefing_tf_prices.jsonl へ追記する。close に加え、取得できる場合は
open/high/low/bid/ask/spread も保存する。fx_briefing の時間足別採点はこの密な
価格系列を判断ジャーナルと結合して将来価格を解決するため、15m/1h/4h/1d の
全時間足が採点可能になる。

判断ロジック・学習・センチメント・カレンダーは一切動かさない(価格取得のみ)。
ネットワーク失敗時もログを残して正常終了し、5分ループを止めない。

使い方:
    .venv/bin/python fx_tf_snapshot.py                       # 既定ペアを1回記録
    .venv/bin/python fx_tf_snapshot.py --symbols USDJPY GBPJPY
    .venv/bin/python fx_tf_snapshot.py --dry-run             # 追記せず内容を表示
    ./fx_tf_snapshot_loop.sh &                               # 5分ごとに自動記録
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

from fx_intel import price_history, technicals

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SYMBOLS = ["USDJPY", "EURUSD"]
# fx_briefing._run_per_timeframe が採点入力に結合する価格専用系列。
# 判断ジャーナル(briefing_tf_journal.jsonl)とは別ファイルにして、
# 価格行(direction 無し)が判断行と混ざらないようにする。
DEFAULT_TF_PRICES_PATH = PROJECT_ROOT / "logs" / "briefing_tf_prices.jsonl"


def collect_closes(
    tech_map: dict[str, technicals.PairTechnicals],
    intervals=technicals.DEFAULT_INTERVALS,
) -> dict[str, dict[str, float | None]]:
    """{symbol: {timeframe: 現在終値}} を組む(取得できた足だけ数値、無い足は None)。"""
    return {
        symbol: {interval: tech.close(interval) for interval in intervals}
        for symbol, tech in tech_map.items()
    }


def collect_price_snapshots(
    tech_map: dict[str, technicals.PairTechnicals],
    intervals=technicals.DEFAULT_INTERVALS,
) -> dict[str, dict[str, dict[str, float] | None]]:
    """{symbol: {timeframe: price snapshot}} を組む(OHLC/bid/ask/spread対応)。"""

    return {
        symbol: {interval: tech.price_snapshot(interval) for interval in intervals}
        for symbol, tech in tech_map.items()
    }


def append_snapshot(path: str | Path, rows: list[dict]) -> None:
    """価格スナップショット行を JSONL へ追記する(1点1行、price_history が読める形)。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="時間足別採点用の価格スナップショットを記録する(価格取得のみ)"
    )
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument(
        "--dry-run", action="store_true", help="ファイルへ追記せず記録内容を表示する"
    )
    args = parser.parse_args(argv)

    symbols = [s.upper().replace("/", "") for s in args.symbols]
    now = datetime.now(UTC)

    tech_map, warnings = technicals.fetch_pair_technicals(symbols)
    for warning in warnings:
        print(f"[warn] {warning}", file=sys.stderr)

    snapshots_by_interval = collect_price_snapshots(tech_map)
    rows = price_history.snapshot_entries(snapshots_by_interval, now=now)
    if not rows:
        # 全時間足の取得に失敗しても異常終了しない(ループを止めないため)
        print("[warn] 価格スナップショットを1点も取得できませんでした", file=sys.stderr)
        return 0

    if args.dry_run:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    append_snapshot(DEFAULT_TF_PRICES_PATH, rows)
    print(
        f"価格スナップショットを記録しました "
        f"({', '.join(symbols)} | {len(rows)}点 | {now.isoformat()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
