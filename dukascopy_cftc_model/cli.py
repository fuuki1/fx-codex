"""dukascopy_cftc_model の CLI エントリポイント(`dcm`)。

サブコマンド:

    dcm fetch   Dukascopy価格 + CFTC COT時系列を取得して data/ に保存(Phase 1)
    dcm qa      取得済みデータの品質チェックと正規化サマリ(Phase 2)
    dcm run     fetch→品質→特徴量→ラベル→Ridge→walk-forward→レポート一括(Phase 6)

各サブコマンドの実処理は対応モジュールに委譲する。CLI自体は薄い引数配線に
徹する(fx_backtester/cli.py と同じ方針)。まだ実装されていない段は
明示的な NotImplementedError を出して、どのフェーズで埋まるかを示す。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import DataConfig, PipelineConfig


def _add_common_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", default="EURUSD", help="通貨ペア(例: EURUSD)")
    parser.add_argument("--start", default="2022-01-01", help="開始日 YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="終了日 YYYY-MM-DD")
    parser.add_argument("--timeframe", default="H1", help="集計する時間足(H1/H4/D1)")
    parser.add_argument("--data-dir", default="data", help="成果物の保存先")
    parser.add_argument("--cache-dir", default="logs/dcm_cache", help="取得キャッシュ先")


def _data_config_from_args(args: argparse.Namespace) -> DataConfig:
    return DataConfig(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        timeframe=args.timeframe,
        cache_dir=Path(args.cache_dir),
        data_dir=Path(args.data_dir),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dcm",
        description="Dukascopy価格 × CFTC COT → Ridge回帰 予測パイプライン",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="Dukascopy価格 + CFTC COT を取得")
    _add_common_data_args(fetch)

    qa = sub.add_parser("qa", help="取得済みデータの品質チェック")
    _add_common_data_args(qa)

    run = sub.add_parser("run", help="全パイプラインを一括実行")
    _add_common_data_args(run)
    run.add_argument("--horizon", type=int, default=24, help="将来リターンのホライズン(バー)")
    run.add_argument(
        "--out", default=None, help="レポートJSONの出力先(既定: data/<sym>_report.json)"
    )
    run.add_argument("--offline", action="store_true", help="取得せずキャッシュ/既存CSVのみ使う")

    return parser


def cmd_fetch(args: argparse.Namespace) -> int:
    from .pipeline import run_fetch

    cfg = _data_config_from_args(args)
    result = run_fetch(cfg)
    print(result.summary())
    return 0


def cmd_qa(args: argparse.Namespace) -> int:
    from .pipeline import run_qa

    cfg = _data_config_from_args(args)
    report = run_qa(cfg)
    print(report.summary())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from .pipeline import run_pipeline

    # CLI引数を DataConfig 全体に反映(symbol だけでなく期間/足/保存先も)。
    cfg = PipelineConfig()
    cfg = replace(cfg, data=_data_config_from_args(args))
    report = run_pipeline(cfg, args=args)
    print(report.summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "fetch": cmd_fetch,
        "qa": cmd_qa,
        "run": cmd_run,
    }
    handler = dispatch[args.command]
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
