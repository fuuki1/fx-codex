"""コマンドライン入口。

  backtest    : 単発バックテスト（指標 JSON を stdout へ）
  walkforward : ウォークフォワード検証サマリ
  optimize    : OOS 検証で選んだ配備用パラメータ（strategy_params.json 用）

例:
  python -m fx_backtester.cli backtest --data prices.csv --strategy ma_cross \
      --param fast_window=20 --param slow_window=60 --param atr_window=14 \
      --param stop_atr_multiple=2.0 --spread-pips USDJPY=0.3 --slippage-pips USDJPY=0.1
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import data as data_mod
from . import validation
from .costs import CostModel
from .runner import run_backtest


def _robust_report(result: Any) -> dict[str, Any]:
    """単発バックテストの堅牢性指標（PSR + モンテカルロ）。"""
    from . import robust

    r = result.bar_returns.to_numpy()
    mc = robust.monte_carlo_bootstrap(r, n_paths=500)
    return {
        "psr": round(robust.probabilistic_sharpe_ratio(r), 4),
        "monte_carlo": {
            k: (round(v, 4) if isinstance(v, float) else v) for k, v in mc.to_dict().items()
        },
    }


def _parse_value(v: str) -> Any:
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _parse_params(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = _parse_value(v.strip())
    return out


def _parse_grid(items: list[str]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = [_parse_value(x.strip()) for x in v.split(",") if x.strip() != ""]
    return out


def _parse_map(items: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip().upper()] = float(v)
    return out


def _symbol(args: argparse.Namespace, spread: dict[str, float], slippage: dict[str, float]) -> str:
    if args.symbol:
        return args.symbol.upper()
    keys = list(spread) or list(slippage)
    return keys[0] if len(keys) == 1 else "USDJPY"


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", required=True)
    p.add_argument("--events", default=None)
    p.add_argument("--strategy", default="ma_cross")
    p.add_argument("--symbol", default=None)
    p.add_argument("--spread-pips", dest="spread", action="append", default=[])
    p.add_argument("--slippage-pips", dest="slippage", action="append", default=[])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fx_backtester")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backtest", help="単発バックテスト")
    _add_common(pb)
    pb.add_argument("--param", action="append", default=[])
    pb.add_argument("--robust", action="store_true",
                    help="PSR とモンテカルロ（定常ブートストラップ）の堅牢性指標も出力")

    for name in ("walkforward", "optimize"):
        pw = sub.add_parser(name, help="ウォークフォワード/最適化")
        _add_common(pw)
        pw.add_argument("--grid", action="append", default=[])
        pw.add_argument("--train", type=int, default=252)
        pw.add_argument("--test", type=int, default=63)
        pw.add_argument("--min-trades", dest="min_trades", type=int, default=20)
        if name == "walkforward":
            pw.add_argument("--folds", action="store_true", help="各フォールド明細も出力")

    args = parser.parse_args(argv)

    spread = _parse_map(args.spread)
    slippage = _parse_map(args.slippage)
    symbol = _symbol(args, spread, slippage)
    cost = CostModel.from_maps(symbol, spread, slippage)

    df = data_mod.load_prices(args.data)
    events = data_mod.load_events(args.events)

    if args.cmd == "backtest":
        params = _parse_params(args.param)
        metrics, result = run_backtest(df, args.strategy, params, cost, events)
        if args.robust:
            metrics["robust"] = _robust_report(result)
        print(json.dumps(metrics, ensure_ascii=False))
        return 0

    grid = _parse_grid(args.grid)
    if args.cmd == "walkforward":
        report = validation.walk_forward(
            df, args.strategy, grid, cost, events,
            train=args.train, test=args.test, min_trades=args.min_trades,
        )
        if not args.folds:
            report = {k: v for k, v in report.items() if k != "folds"}
        print(json.dumps(report, ensure_ascii=False))
        return 0

    # optimize
    result = validation.optimize(
        df, args.strategy, grid, cost, events,
        train=args.train, test=args.test, min_trades=args.min_trades,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
