"""時間足別採点用の価格スナップショットを5分ごとに記録する軽量スクリプト。

時間足別モード(fx_briefing.py --per-timeframe)の自己採点は、各判断を
「記録時刻 + その足の主ホライズン」時点の実勢価格と突き合わせる。将来価格は
ジャーナルの後続エントリ(源A)から取る。通常のFXシグナルボード運用では判断と価格が
5分ごとに記録されるためこの補助スクリプトは不要だが、Discord通知を止めたまま
学習用価格だけを継続収集したい場合に使う。

このスクリプトは判断・Discord通知とは切り離し、OANDA v20 または IBKR paper
から最新の完了済みM5 bid/ask OHLCを専用価格系列へ追記する。
形成中足を使わず、足開始・終了時刻を残すため、判断前のhigh/lowが将来経路へ
混ざることを防げる。既存のTradingView方式は診断用に明示指定した場合だけ使える。

判断ロジック・学習・センチメント・カレンダーは一切動かさない(価格取得のみ)。
終了コードの約束(launchd/監視が「見かけ上の成功」に騙されないため):
    0  … 要求した全銘柄・全時間足を保存できた。
    3  … 1点でも欠けた。取得できた点は証拠として保存するが、launchdには非zeroで
          不完全取得を伝え、鮮度監視は完全なcapture slotまで criticalを維持する。
    1  … 引数不正など(argparse)。
一時障害でもプロセスはクラッシュせず 3 で戻るだけなので、ループは止まらない。

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
DEFAULT_SYMBOLS = ["GBPUSD", "EURUSD", "USDJPY"]
DEFAULT_PROVIDER = "tradingview"
# fx_briefing._run_per_timeframe が採点入力に結合する価格専用系列。
# 判断ジャーナル(briefing_tf_journal.jsonl)とは別ファイルにして、
# 価格行(direction 無し)が判断行と混ざらないようにする。
DEFAULT_TF_PRICES_PATH = PROJECT_ROOT / "logs" / "briefing_tf_prices.jsonl"

# 全取得が一時障害で失敗し1点も保存できなかった時の終了コード。
# 0(成功)でも 1(引数不正)でもない値にして、launchd/監視が一時障害を
# 「見かけ上の成功」と誤認しないようにする。
EXIT_TRANSIENT_FAILURE = 3


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
    price_history.append_snapshot_entries(path, rows)


def had_transient_failure(tech_map: dict[str, technicals.PairTechnicals]) -> bool:
    """いずれかの銘柄・時間足で一時障害(429/ネットワーク等)が起きたか。

    恒久的な空data(取れたが行が無い)と、取得そのものが一時的に失敗したケースを
    区別するために使う。全滅時の終了コード判定に用いる。
    """
    return any(tech.transient_failures for tech in tech_map.values())


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
    tech_map: dict[str, technicals.PairTechnicals] = {}
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
        # Acquisition completion is the earliest instant this process can use the
        # snapshot. Timestamping before the network call would create false PIT history.
        now = datetime.now(UTC)
        rows = price_history.snapshot_entries(snapshots_by_interval, now=now)

    for warning in warnings:
        print(f"[warn] {warning}", file=sys.stderr)
    expected_points = len(symbols) * len(technicals.DEFAULT_INTERVALS)
    if not rows:
        transient = args.provider in {"oanda", "ibkr"} or had_transient_failure(tech_map)
        reason = "一時障害で" if transient else ""
        print(
            f"[error] 全時間足・全銘柄が{reason}取得できませんでした"
            f"(exit {EXIT_TRANSIENT_FAILURE}、次周期で再試行)",
            file=sys.stderr,
        )
        return EXIT_TRANSIENT_FAILURE

    if args.dry_run:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0 if len(rows) == expected_points else EXIT_TRANSIENT_FAILURE

    append_snapshot(DEFAULT_TF_PRICES_PATH, rows)
    print(
        f"価格スナップショットを記録しました "
        f"({args.provider} | {', '.join(symbols)} | {len(rows)}点 | {now.isoformat()})"
    )
    if len(rows) != expected_points:
        print(
            f"[error] 価格スナップショットが不完全です "
            f"({len(rows)}/{expected_points}点、exit {EXIT_TRANSIENT_FAILURE})",
            file=sys.stderr,
        )
        return EXIT_TRANSIENT_FAILURE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
