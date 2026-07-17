"""時間足別採点用の価格スナップショットを5分ごとに記録する軽量スクリプト。

時間足別モード(fx_briefing.py --per-timeframe)の自己採点は、各判断を
「記録時刻 + その足の主ホライズン」時点の実勢価格と突き合わせる。将来価格は
ジャーナルの後続エントリ(源A)から取るが、判断ジャーナルは毎時しか追記されない
ため、短い足(特に 15m: 主ホライズン15分)は採点窓[9,21分]に入る点が
永久に得られず、学習が回らない。

このスクリプトは判断・Discord通知とは切り離し、OANDA v20 または IBKR paper
から最新の完了済みM5 bid/ask OHLCを取得して専用の価格系列へ追記する。
形成中足を使わず、足開始・終了時刻を残すため、判断前のhigh/lowが将来経路へ
混ざることを防げる。既存のTradingView方式は診断用に明示指定した場合だけ使える。

判断ロジック・学習・センチメント・カレンダーは一切動かさない(価格取得のみ)。
ネットワーク失敗時は異常終了コードとログを残すが、外側の5分ループは継続する。

使い方:
    .venv/bin/python fx_tf_snapshot.py                       # OANDAで既定ペアを1回記録
    .venv/bin/python fx_tf_snapshot.py --symbols USDJPY GBPJPY
    .venv/bin/python fx_tf_snapshot.py --dry-run             # 追記せず内容を表示
    .venv/bin/python fx_tf_snapshot.py --provider ibkr       # IBKR paper(read-only)
    .venv/bin/python fx_tf_snapshot.py --provider tradingview # 旧方式(診断用)
    ./fx_tf_snapshot_loop.sh &                               # 5分ごとに自動記録

必要な.env:
    OANDA_API_TOKEN=...
    OANDA_ENVIRONMENT=practice   # または live。価格取得のみで注文は出さない
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

from fx_intel import ibkr_prices, oanda_prices, price_history, technicals

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SYMBOLS = ["USDJPY", "EURUSD", "GBPUSD"]
DEFAULT_PROVIDER = "oanda"
# fx_briefing._run_per_timeframe が採点入力に結合する価格専用系列。
# 判断ジャーナル(briefing_tf_journal.jsonl)とは別ファイルにして、
# 価格行(direction 無し)が判断行と混ざらないようにする。
DEFAULT_TF_PRICES_PATH = PROJECT_ROOT / "logs" / "briefing_tf_prices.jsonl"


def configured_provider() -> str:
    """launchdでも.envの価格provider設定を読めるようにする。"""

    value = os.environ.get("FX_PRICE_PROVIDER")
    if value:
        return value.strip().lower()
    try:
        lines = (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return DEFAULT_PROVIDER
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("FX_PRICE_PROVIDER="):
            return stripped.split("=", 1)[1].strip().strip("\"'").lower()
    return DEFAULT_PROVIDER


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
        "--provider",
        choices=("oanda", "ibkr", "tradingview"),
        default=configured_provider(),
        help="価格取得元。oanda/ibkrは完了bid/ask足、tradingviewは診断用",
    )
    parser.add_argument(
        "--oanda-environment",
        choices=("practice", "live"),
        default=None,
        help="OANDA接続先。.envのOANDA_ENVIRONMENTを上書き",
    )
    parser.add_argument(
        "--granularity",
        default=None,
        help="OANDA採点足の粒度(既定M5)。通常は変更しない",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="ファイルへ追記せず記録内容を表示する"
    )
    args = parser.parse_args(argv)

    symbols = [s.upper().replace("/", "") for s in args.symbols]
    now = datetime.now(UTC)

    if args.provider == "oanda":
        try:
            config = oanda_prices.OandaPriceConfig.from_env(
                project_root=PROJECT_ROOT,
                environment=args.oanda_environment,
                granularity=args.granularity,
            )
        except ValueError as error:
            # close-onlyへ黙ってフォールバックすると品質改善したように見えてしまう。
            print(f"[error] {error}", file=sys.stderr)
            return 2
        rows, warnings = oanda_prices.fetch_completed_bid_ask_rows(
            symbols,
            config,
            target_timeframes=technicals.DEFAULT_INTERVALS,
            now=now,
        )
    elif args.provider == "ibkr":
        try:
            config = ibkr_prices.IbkrPriceConfig.from_env(project_root=PROJECT_ROOT)
        except ValueError as error:
            print(f"[error] {error}", file=sys.stderr)
            return 2
        rows, warnings = ibkr_prices.fetch_completed_bid_ask_rows(
            symbols,
            config,
            target_timeframes=technicals.DEFAULT_INTERVALS,
            now=now,
        )
    else:
        tech_map, warnings = technicals.fetch_pair_technicals(symbols)
        snapshots_by_interval = collect_price_snapshots(tech_map)
        rows = price_history.snapshot_entries(snapshots_by_interval, now=now)

    for warning in warnings:
        print(f"[warn] {warning}", file=sys.stderr)
    if not rows:
        # 全時間足の取得に失敗しても異常終了しない(ループを止めないため)
        print("[error] 価格スナップショットを1点も取得できませんでした", file=sys.stderr)
        return 1 if args.provider in {"oanda", "ibkr"} else 0

    if args.dry_run:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    append_snapshot(DEFAULT_TF_PRICES_PATH, rows)
    print(
        f"価格スナップショットを記録しました "
        f"({args.provider} | {', '.join(symbols)} | {len(rows)}点 | {now.isoformat()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
