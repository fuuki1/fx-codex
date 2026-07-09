"""パイプラインのオーケストレーション(段を順に呼ぶ薄い層)。

Phase 1 で run_fetch を実体化。run_qa/run_pipeline は後続フェーズで埋める。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, UTC
from pathlib import Path

import pandas as pd

from fx_backtester.models import instrument_for, normalize_symbol

from .cftc import fetch_cot_history
from .config import DataConfig, PipelineConfig
from .dukascopy import fetch_prices


@dataclass
class FetchResult:
    """fetch の成果物(保存先パスと行数)。"""

    symbol: str
    timeframe: str
    price_path: Path
    prices: pd.DataFrame
    cot_paths: dict[str, Path]
    cot: dict[str, pd.DataFrame]

    def summary(self) -> str:
        lines = [
            f"[fetch] {self.symbol} {self.timeframe}",
            f"  価格バー: {len(self.prices)} 本 → {self.price_path}",
        ]
        if not self.prices.empty:
            lines.append(f"  期間: {self.prices.index.min()} 〜 {self.prices.index.max()}")
        for ccy, frame in self.cot.items():
            lines.append(f"  COT[{ccy}]: {len(frame)} 週 → {self.cot_paths[ccy]}")
        return "\n".join(lines)


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value).date()


def _currencies_for(symbol: str) -> list[str]:
    """ペアの base/quote 両通貨(COTを両方引く)。"""
    inst = instrument_for(symbol)
    return [inst.base, inst.quote]


def run_fetch(cfg: DataConfig, progress: bool = True) -> FetchResult:
    """Dukascopy価格 + CFTC COT時系列を取得し data/ に保存する。"""
    symbol = normalize_symbol(cfg.symbol)
    start_dt = datetime.combine(_parse_date(cfg.start), datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(_parse_date(cfg.end), datetime.max.time(), tzinfo=UTC)

    prices = fetch_prices(
        symbol,
        start_dt,
        end_dt,
        cfg.timeframe,
        cache_dir=cfg.cache_dir,
        progress=progress,
    )

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    price_path = cfg.data_dir / f"{symbol}_{cfg.timeframe}.csv"
    _save_prices_csv(prices, price_path, symbol)

    cot: dict[str, pd.DataFrame] = {}
    cot_paths: dict[str, Path] = {}
    cot_start = _parse_date(cfg.start)
    for ccy in _currencies_for(symbol):
        frame = fetch_cot_history(
            ccy,
            cot_start,
            cache_dir=cfg.cache_dir,
            cache_ttl_hours=cfg.cache_ttl_hours,
        )
        cot[ccy] = frame
        cot_path = cfg.data_dir / f"COT_{ccy}.csv"
        frame.to_csv(cot_path, index=False)
        cot_paths[ccy] = cot_path

    return FetchResult(
        symbol=symbol,
        timeframe=cfg.timeframe,
        price_path=price_path,
        prices=prices,
        cot_paths=cot_paths,
        cot=cot,
    )


def _save_prices_csv(prices: pd.DataFrame, path: Path, symbol: str) -> None:
    """fx_backtester.data.load_price_csv が読めるスキーマで保存する。"""
    out = prices.copy()
    out.insert(0, "symbol", symbol)
    out.index.name = "timestamp"
    out.to_csv(path)


def load_fetched(cfg: DataConfig) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """data/ に保存済みの価格CSV + COT CSV を読み込む(offline経路の共通処理)。"""
    symbol = normalize_symbol(cfg.symbol)
    price_path = cfg.data_dir / f"{symbol}_{cfg.timeframe}.csv"
    if not price_path.exists():
        raise FileNotFoundError(
            f"{price_path} が無い。先に `dcm fetch --symbol {symbol}` を実行してください。"
        )
    from fx_backtester.data import load_price_csv

    prices = load_price_csv(price_path)[symbol]

    cot: dict[str, pd.DataFrame] = {}
    for ccy in _currencies_for(symbol):
        cot_path = cfg.data_dir / f"COT_{ccy}.csv"
        if cot_path.exists():
            frame = pd.read_csv(cot_path, parse_dates=["report_date"])
        else:
            frame = pd.DataFrame(columns=["report_date"])
        cot[ccy] = frame
    return prices, cot


def run_qa(cfg: DataConfig):  # noqa: ANN201
    from .quality import build_report

    prices, cot = load_fetched(cfg)
    return build_report(prices, cot, cfg.timeframe)


def run_pipeline(cfg: PipelineConfig, args: argparse.Namespace | None = None):  # noqa: ANN201
    """全パイプラインを実行する: 取得/読込 → 品質 → 特徴量 → ラベル → Ridge
    walk-forward → レポート。

    args.offline なら取得せず data/ の既存CSVを読む。args.horizon で将来
    リターンのホライズンを上書き。返り値は PipelineReport。
    """
    import json

    from .features import build_feature_matrix
    from .labels import align_xy, build_labels, future_return
    from .quality import build_report as build_quality_report
    from .quality import normalize_cot, normalize_prices
    from .report import build_report as build_pipeline_report
    from .walk_forward import run_walk_forward

    offline = bool(getattr(args, "offline", False)) if args is not None else False
    horizon = (
        int(getattr(args, "horizon", cfg.labels.horizon))
        if args is not None
        else cfg.labels.horizon
    )
    cfg = cfg.with_labels(horizon=horizon)

    symbol = normalize_symbol(cfg.data.symbol)

    # 1. 取得 or 既存読込
    if offline:
        prices, cot_raw = load_fetched(cfg.data)
    else:
        fetched = run_fetch(cfg.data, progress=True)
        prices, cot_raw = fetched.prices, fetched.cot

    # 2. 品質チェック + 正規化
    quality = build_quality_report(prices, cot_raw, cfg.data.timeframe)
    print(quality.summary())
    prices = normalize_prices(prices)
    cot = {ccy: normalize_cot(frame) for ccy, frame in cot_raw.items()}

    # 3. 特徴量 + 4. ラベル + 実現将来リターン
    X = build_feature_matrix(prices, cot, symbol, cfg.features)
    y = build_labels(prices, cfg.labels)
    fret = future_return(prices, cfg.labels.horizon)  # signalの実現リターン(生log-return)

    X_aligned, y_aligned = align_xy(X, y)
    fret_aligned = fret.reindex(X_aligned.index)
    # 実現将来リターンにNaNが残る行は3構造から一括除去して整合を保つ
    # (通常は y と同じホライズンなので発生しないが、vol正規化ラベル等で
    #  y 側だけ残りうるケースの保険)。
    valid = fret_aligned.notna()
    if not valid.all():
        X_aligned = X_aligned[valid]
        y_aligned = y_aligned[valid]
        fret_aligned = fret_aligned[valid]

    if len(X_aligned) < cfg.walk_forward.train_bars + cfg.walk_forward.test_bars:
        raise ValueError(
            f"有効サンプルが {len(X_aligned)} 本しかなく walk-forward に不足"
            f"(train {cfg.walk_forward.train_bars} + test {cfg.walk_forward.test_bars} 必要)。"
            "より長い期間を fetch してください。"
        )

    # 5. Ridge walk-forward バックテスト
    wf = run_walk_forward(X_aligned, y_aligned, fret_aligned, cfg)

    # 6. レポート集約
    provenance = {
        "source_prices": "Dukascopy datafeed (tick→OHLCV)",
        "source_cot": "CFTC Socrata COT (週次)",
        "period": f"{cfg.data.start} 〜 {cfg.data.end}",
        "samples_used": len(X_aligned),
        "n_features": X_aligned.shape[1],
        "alpha_grid": cfg.walk_forward.alpha_grid,
    }
    report = build_pipeline_report(
        symbol, cfg.data.timeframe, cfg.labels.horizon, wf, quality, provenance
    )

    out_path = None
    if args is not None and getattr(args, "out", None):
        out_path = Path(args.out)
    else:
        out_path = cfg.data.data_dir / f"{symbol}_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nレポートJSON: {out_path}")

    return report
