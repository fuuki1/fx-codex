"""時間足別採点用の価格スナップショットを5分ごとに記録する軽量スクリプト。

時間足別モード(fx_briefing.py --per-timeframe)の自己採点は、各判断を
「記録時刻 + その足の主ホライズン」時点の実勢価格と突き合わせる。将来価格は
ジャーナルの後続エントリ(源A)から取る。通常のFXシグナルボード運用では判断と価格が
5分ごとに記録されるためこの補助スクリプトは不要だが、Discord通知を止めたまま
学習用価格だけを継続収集したい場合に使う。

このスクリプトは判断・Discord通知とは切り離し、TradingView から各時間足の
現在価格スナップショットを5分ごとに取得して専用の価格系列
logs/briefing_tf_prices.jsonl へ追記する。close に加え、取得できる場合は
open/high/low/bid/ask/spread も保存する。fx_briefing の時間足別採点はこの密な
価格系列を判断ジャーナルと結合して将来価格を解決するため、15m/1h/4h/1d の
全時間足が採点可能になる。

判断ロジック・学習・センチメント・カレンダーは一切動かさない(価格取得のみ)。

終了コードの約束(launchd/監視が「見かけ上の成功」に騙されないため):
    0  … 1点以上を保存できた(部分成功も成功扱い。1つでも取れれば前進)。
    3  … 全時間足・全銘柄が一時障害(429/ネットワーク/HTTP/非JSON)で取れず、
          1点も保存できなかった。launchdはこの非zeroで失敗を認識し、鮮度監視は
          次に成功するまで critical を維持する。5分ループは次周期で再試行する。
    1  … 引数不正など(argparse)。
一時障害でもプロセスはクラッシュせず 3 で戻るだけなので、ループは止まらない。

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
DEFAULT_SYMBOLS = ["GBPUSD", "EURUSD", "USDJPY"]
# fx_briefing._run_per_timeframe が採点入力に結合する価格専用系列。
# 判断ジャーナル(briefing_tf_journal.jsonl)とは別ファイルにして、
# 価格行(direction 無し)が判断行と混ざらないようにする。
DEFAULT_TF_PRICES_PATH = PROJECT_ROOT / "logs" / "briefing_tf_prices.jsonl"

# 全取得が一時障害で失敗し1点も保存できなかった時の終了コード。
# 0(成功)でも 1(引数不正)でもない値にして、launchd/監視が一時障害を
# 「見かけ上の成功」と誤認しないようにする。
EXIT_TRANSIENT_FAILURE = 3


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
        "--dry-run", action="store_true", help="ファイルへ追記せず記録内容を表示する"
    )
    args = parser.parse_args(argv)

    symbols = [s.upper().replace("/", "") for s in args.symbols]
    tech_map, warnings = technicals.fetch_pair_technicals(symbols)
    for warning in warnings:
        print(f"[warn] {warning}", file=sys.stderr)

    snapshots_by_interval = collect_price_snapshots(tech_map)
    # Acquisition completion is the earliest instant this process can use the
    # snapshot. Timestamping before the network call would create false PIT history.
    now = datetime.now(UTC)
    rows = price_history.snapshot_entries(snapshots_by_interval, now=now)
    if not rows:
        # 1点も取れなかった。一時障害(429/ネットワーク等)が原因なら非zeroで戻り、
        # launchd/監視に失敗を伝える(次周期で再試行、鮮度は成功まで critical)。
        # クラッシュはせず戻るだけなので5分ループは止まらない。部分成功はこの分岐に来ない。
        if had_transient_failure(tech_map):
            print(
                "[error] 全時間足・全銘柄が一時障害で取得できませんでした"
                f"(exit {EXIT_TRANSIENT_FAILURE}、次周期で再試行)",
                file=sys.stderr,
            )
            return EXIT_TRANSIENT_FAILURE
        # 一時障害の記録が無いのに1点も無いのは想定外(空data等)。同様に非zeroで扱う。
        print(
            "[error] 価格スナップショットを1点も取得できませんでした"
            f"(exit {EXIT_TRANSIENT_FAILURE})",
            file=sys.stderr,
        )
        return EXIT_TRANSIENT_FAILURE

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
