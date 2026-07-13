from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from fx_backtester.analysis import RunAnalysisConfig, analyze_run_artifacts
from fx_backtester.artifacts import audit_run_artifacts, write_backtest_run_artifacts
from fx_backtester.data import (
    filter_economic_events_by_date,
    filter_price_data_by_date,
    load_economic_events_for_backtest,
    load_price_csvs,
)
from fx_backtester.engine import BacktestConfig, BacktestEngine
from fx_backtester.execution import ExecutionConfig
from fx_backtester.qa import DataQualityConfig, validate_price_data
from fx_backtester.research import PRESETS, write_research_pack
from fx_backtester.risk import RiskConfig
from fx_backtester.strategies import DEFAULT_PARAM_GRIDS, STRATEGY_REGISTRY
from fx_backtester.strategies.filters import (
    FilteredStrategy,
    NoTradeFilterConfig,
    RegimeFilterConfig,
)
from fx_backtester.tradingview import TradingViewWebhookConfig, run_tradingview_webhook_server
from fx_backtester.validation import validate_backtest_inputs, validate_trade_log_contract
from fx_backtester.walk_forward import WalkForwardConfig, WalkForwardValidator

DEFAULT_CLI_VALUES: dict[str, Any] = {
    "initial_cash": 100_000.0,
    "risk_per_trade": 0.01,
    "risk_cap": 0.01,
    "max_daily_loss": 0.02,
    "max_weekly_loss": None,
    "max_monthly_drawdown": None,
    "monthly_profit_target": None,
    "hard_drawdown": None,
    "min_stop_pips": 5.0,
    "max_leverage": 10.0,
    "max_currency_exposure": None,
    "max_position_units": None,
    "allow_fractional_units": False,
    "max_open_positions": None,
    "cooldown_bars_after_stop": 0,
    "commission_per_million": 30.0,
    "fixed_fee": 0.0,
    "minimum_fee": 0.0,
    "spread_pips": [],
    "slippage_pips": [],
    "spread_time_multiplier": ["21=2.0", "22=1.5"],
    "slippage_time_multiplier": ["21=2.0", "22=1.5"],
    "no_trade_before": 30,
    "no_trade_after": 30,
    "min_event_impact": "medium",
    "trading_start": None,
    "trading_end": None,
    "blocked_weekday": [],
    "conversion_rate": [],
    "close_positions_on_daily_stop": True,
    "close_positions_on_portfolio_stop": True,
    "force_close_on_end": True,
    "train_bars": 500,
    "test_bars": 100,
    "step_bars": None,
    "max_params": 20,
    "purge_bars": 0,
    "embargo_bars": 0,
    "regime_filter": True,
    "regime_window": 200,
    "regime_slope_window": 24,
    "regime_min_atr_percentile": 0.10,
    "regime_max_atr_percentile": 0.95,
    "signal_no_trade_filter": True,
    "blocked_entry_hour": ["21,22"],
    "max_spread_multiple": 2.5,
    "spread_lookback": 48,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FX backtesting framework")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest_parser = _add_common_arguments(subparsers.add_parser("backtest"))
    backtest_parser.add_argument("--output-trades", help="Optional path to write trade CSV")
    backtest_parser.add_argument(
        "--output-trade-log",
        dest="output_trades",
        help="Alias for --output-trades; use trade_log.csv for realistic fill logs",
    )
    backtest_parser.add_argument("--output-equity", help="Optional path to write equity CSV")
    backtest_parser.add_argument("--output-metrics", help="Optional path to write metrics JSON")
    backtest_parser.add_argument(
        "--output-dir",
        help="Write production-grade run artifacts: manifest, config, QA, metrics, equity, trade_log",
    )
    backtest_parser.add_argument(
        "--expected-frequency", help="Expected pandas frequency for artifact QA"
    )
    backtest_parser.add_argument("--max-missing-pct", type=float, default=0.0005)

    walk_parser = _add_common_arguments(subparsers.add_parser("walk-forward"))
    walk_parser.add_argument("--train-bars", type=int, default=500)
    walk_parser.add_argument("--test-bars", type=int, default=100)
    walk_parser.add_argument("--step-bars", type=int)
    walk_parser.add_argument("--purge-bars", type=int, default=DEFAULT_CLI_VALUES["purge_bars"])
    walk_parser.add_argument("--embargo-bars", type=int, default=DEFAULT_CLI_VALUES["embargo_bars"])
    walk_parser.add_argument("--max-params", type=int, default=20)
    walk_parser.add_argument(
        "--output-summary", help="Optional path to write walk-forward summary CSV"
    )
    walk_parser.add_argument(
        "--grid",
        action="append",
        default=[],
        help="Parameter grid item like fast_window=10,20. Can be repeated.",
    )

    research_parser = subparsers.add_parser("research-pack")
    research_parser.add_argument(
        "--output-dir",
        default="research_pack",
        help="Directory for public source catalog, major event CSV, config JSON, and notes",
    )
    research_parser.add_argument(
        "--source-report",
        help="Optional local Markdown report to copy into the generated research pack",
    )

    qa_parser = subparsers.add_parser("qa-data")
    qa_parser.add_argument("--data", nargs="+", required=True, help="CSV file(s) with OHLC data")
    qa_parser.add_argument(
        "--expected-frequency", help="Expected pandas frequency, e.g. 1min, 15min, h"
    )
    qa_parser.add_argument("--max-missing-pct", type=float, default=0.0005)
    qa_parser.add_argument("--start-date", help="Inclusive start timestamp/date for QA")
    qa_parser.add_argument("--end-date", help="Inclusive end timestamp/date for QA")
    qa_parser.add_argument("--output", help="Optional path to write QA CSV")

    audit_parser = subparsers.add_parser("audit-run")
    audit_parser.add_argument(
        "--run-dir", required=True, help="Backtest artifact directory to audit"
    )

    analyze_parser = subparsers.add_parser("analyze-run")
    analyze_parser.add_argument(
        "--run-dir", required=True, help="Backtest artifact directory to analyze"
    )
    analyze_parser.add_argument(
        "--output-dir", help="Directory for analysis artifacts; defaults to run-dir"
    )
    analyze_parser.add_argument("--oos-ratio", type=float, default=0.30)
    analyze_parser.add_argument("--monte-carlo-paths", type=int, default=2_000)
    analyze_parser.add_argument("--monte-carlo-seed", type=int, default=42)
    analyze_parser.add_argument(
        "--ruin-threshold-pct",
        type=float,
        default=30.0,
        help="Ruin threshold as percent drawdown from initial equity, e.g. 30",
    )
    analyze_parser.add_argument(
        "--cost-multiplier",
        action="append",
        default=None,
        help="Cost multiplier to include, e.g. 1.5. Can be repeated.",
    )
    analyze_parser.add_argument("--min-period-days", type=int, default=365)
    analyze_parser.add_argument("--min-oos-trades", type=int, default=30)
    analyze_parser.add_argument("--min-walk-forward-folds", type=int, default=3)
    analyze_parser.add_argument("--min-forward-days", type=int, default=30)
    analyze_parser.add_argument(
        "--monthly-target-return",
        type=float,
        default=0.08,
        help="Required monthly return gate, as 0.08 or 8 for 8%%.",
    )
    analyze_parser.add_argument("--walk-forward-summary", help="Optional walk-forward summary CSV")
    analyze_parser.add_argument("--forward-trades", help="Optional forward/paper trade_log CSV")
    analyze_parser.add_argument("--no-html", action="store_false", dest="write_html", default=True)

    tradingview_parser = subparsers.add_parser("tradingview-webhook")
    tradingview_parser.add_argument("--host", default="127.0.0.1")
    tradingview_parser.add_argument("--port", type=int, default=8080)
    tradingview_parser.add_argument("--path", default="/webhook/tradingview")
    tradingview_parser.add_argument(
        "--output",
        default="runs/tradingview_alerts.jsonl",
        help="JSONL path for received TradingView alerts",
    )
    tradingview_parser.add_argument("--secret", help="Shared secret expected in the alert JSON")
    tradingview_parser.add_argument(
        "--secret-env",
        help="Environment variable that contains the shared secret",
    )
    tradingview_parser.add_argument("--max-body-bytes", type=int, default=65_536)

    args = parser.parse_args(argv)
    if args.command == "backtest":
        return _run_backtest(args)
    if args.command == "walk-forward":
        return _run_walk_forward(args)
    if args.command == "research-pack":
        return _run_research_pack(args)
    if args.command == "qa-data":
        return _run_qa_data(args)
    if args.command == "audit-run":
        return _run_audit_run(args)
    if args.command == "analyze-run":
        return _run_analyze_run(args)
    if args.command == "tradingview-webhook":
        return _run_tradingview_webhook(args)
    raise ValueError(args.command)


def _add_common_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--data", nargs="+", required=True, help="CSV file(s) with OHLC data")
    parser.add_argument("--events", help="Economic calendar CSV")
    parser.add_argument("--strategy", choices=sorted(STRATEGY_REGISTRY), required=True)
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="Apply a researched conservative configuration unless an option is explicitly overridden",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=None,
        help="Strategy parameter like fast_window=20",
    )
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_CLI_VALUES["initial_cash"])
    parser.add_argument(
        "--risk-per-trade", type=float, default=DEFAULT_CLI_VALUES["risk_per_trade"]
    )
    parser.add_argument("--risk-cap", type=float, default=DEFAULT_CLI_VALUES["risk_cap"])
    parser.add_argument(
        "--max-daily-loss", type=float, default=DEFAULT_CLI_VALUES["max_daily_loss"]
    )
    parser.add_argument("--max-weekly-loss", type=float)
    parser.add_argument("--max-monthly-drawdown", type=float)
    parser.add_argument(
        "--monthly-profit-target",
        type=float,
        help="Lock new risk after monthly profit target is reached, as 0.08 or 8 for 8%%.",
    )
    parser.add_argument("--hard-drawdown", type=float)
    parser.add_argument("--min-stop-pips", type=float, default=DEFAULT_CLI_VALUES["min_stop_pips"])
    parser.add_argument("--max-leverage", type=float, default=DEFAULT_CLI_VALUES["max_leverage"])
    parser.add_argument("--max-currency-exposure", type=float)
    parser.add_argument("--max-position-units", type=float)
    parser.add_argument("--allow-fractional-units", action="store_true")
    parser.add_argument("--max-open-positions", type=int)
    parser.add_argument(
        "--cooldown-bars-after-stop",
        type=int,
        default=DEFAULT_CLI_VALUES["cooldown_bars_after_stop"],
    )
    parser.add_argument(
        "--commission-per-million",
        type=float,
        default=DEFAULT_CLI_VALUES["commission_per_million"],
    )
    parser.add_argument("--fixed-fee", type=float, default=DEFAULT_CLI_VALUES["fixed_fee"])
    parser.add_argument("--minimum-fee", type=float, default=DEFAULT_CLI_VALUES["minimum_fee"])
    parser.add_argument(
        "--spread-pips",
        action="append",
        default=None,
        help="Override spread like EURUSD=0.6. Can be repeated.",
    )
    parser.add_argument(
        "--slippage-pips",
        action="append",
        default=None,
        help="Override slippage like EURUSD=0.1. Can be repeated.",
    )
    parser.add_argument(
        "--spread-time-multiplier",
        action="append",
        default=None,
        help="Time-varying spread multiplier like 21=2.0. Can be repeated.",
    )
    parser.add_argument(
        "--slippage-time-multiplier",
        action="append",
        default=None,
        help="Time-varying slippage multiplier like 21=2.0. Can be repeated.",
    )
    parser.add_argument(
        "--no-trade-before", type=int, default=DEFAULT_CLI_VALUES["no_trade_before"]
    )
    parser.add_argument("--no-trade-after", type=int, default=DEFAULT_CLI_VALUES["no_trade_after"])
    parser.add_argument(
        "--min-event-impact",
        choices=["low", "medium", "high"],
        default=DEFAULT_CLI_VALUES["min_event_impact"],
    )
    parser.add_argument("--trading-start", help="Entry session start time, HH:MM")
    parser.add_argument("--trading-end", help="Entry session end time, HH:MM")
    parser.add_argument(
        "--blocked-weekday",
        action="append",
        default=None,
        help="Block entries on weekday 0-6 or mon-sun. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--conversion-rate",
        action="append",
        default=None,
        help="Static conversion rate like USDJPY=150.0. Can be repeated.",
    )
    parser.add_argument("--start-date", help="Inclusive backtest start timestamp/date")
    parser.add_argument("--end-date", help="Inclusive backtest end timestamp/date")
    parser.add_argument(
        "--disable-regime-filter",
        action="store_false",
        dest="regime_filter",
        default=DEFAULT_CLI_VALUES["regime_filter"],
    )
    parser.add_argument("--regime-window", type=int, default=DEFAULT_CLI_VALUES["regime_window"])
    parser.add_argument(
        "--regime-slope-window",
        type=int,
        default=DEFAULT_CLI_VALUES["regime_slope_window"],
    )
    parser.add_argument(
        "--regime-min-atr-percentile",
        type=float,
        default=DEFAULT_CLI_VALUES["regime_min_atr_percentile"],
    )
    parser.add_argument(
        "--regime-max-atr-percentile",
        type=float,
        default=DEFAULT_CLI_VALUES["regime_max_atr_percentile"],
    )
    parser.add_argument(
        "--disable-signal-no-trade-filter",
        action="store_false",
        dest="signal_no_trade_filter",
        default=DEFAULT_CLI_VALUES["signal_no_trade_filter"],
    )
    parser.add_argument(
        "--blocked-entry-hour",
        action="append",
        default=None,
        help="Block new entries by hour 0-23. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--max-spread-multiple",
        type=float,
        default=DEFAULT_CLI_VALUES["max_spread_multiple"],
    )
    parser.add_argument(
        "--spread-lookback", type=int, default=DEFAULT_CLI_VALUES["spread_lookback"]
    )
    parser.add_argument(
        "--no-close-on-daily-stop",
        action="store_false",
        dest="close_positions_on_daily_stop",
        default=True,
    )
    parser.add_argument(
        "--no-close-on-portfolio-stop",
        action="store_false",
        dest="close_positions_on_portfolio_stop",
        default=True,
    )
    parser.add_argument(
        "--keep-open-on-end",
        action="store_false",
        dest="force_close_on_end",
        default=True,
    )
    return parser


def _run_backtest(args: argparse.Namespace) -> int:
    config = _build_config(args)
    data = filter_price_data_by_date(load_price_csvs(args.data), args.start_date, args.end_date)
    events = filter_economic_events_by_date(
        load_economic_events_for_backtest(args.events, data),
        args.start_date,
        args.end_date,
        minutes_before=config.no_trade_minutes_before,
        minutes_after=config.no_trade_minutes_after,
    )
    validation = validate_backtest_inputs(data, config)
    validation.raise_for_errors()

    strategy_params = _build_strategy_params(args.param)
    strategy = _build_strategy_from_args(args, strategy_params)
    engine = BacktestEngine(strategy, config, events)
    result = engine.run(data)
    validate_trade_log_contract(result.trades).raise_for_errors()

    if args.output_trades:
        result.trades.to_csv(args.output_trades, index=False)
    if args.output_equity:
        result.equity_curve.to_csv(args.output_equity)
    metrics = _jsonable(result.metrics)
    if args.output_metrics:
        Path(args.output_metrics).write_text(
            json.dumps(metrics, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if args.output_dir:
        written = write_backtest_run_artifacts(
            args.output_dir,
            data_paths=list(args.data),
            events_path=args.events,
            strategy_name=strategy.name,
            strategy_params=strategy_params,
            config=config,
            result=result,
            data=data,
            command=json.dumps(vars(args), sort_keys=True, default=str),
            qa_config=DataQualityConfig(
                expected_frequency=args.expected_frequency,
                max_missing_pct=args.max_missing_pct,
            ),
        )
        metrics["artifact_manifest"] = str(written["manifest"])

    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    return 0


def _run_walk_forward(args: argparse.Namespace) -> int:
    base_config = _build_config(args)
    data = filter_price_data_by_date(load_price_csvs(args.data), args.start_date, args.end_date)
    events = filter_economic_events_by_date(
        load_economic_events_for_backtest(args.events, data),
        args.start_date,
        args.end_date,
        minutes_before=base_config.no_trade_minutes_before,
        minutes_after=base_config.no_trade_minutes_after,
    )
    strategy_factory = _strategy_factory_from_args(args)
    parameter_grid = _build_grid(args.strategy, args.grid)
    validate_backtest_inputs(data, base_config).raise_for_errors()

    def engine_factory(strategy: Any) -> BacktestEngine:
        return BacktestEngine(strategy, base_config, events)

    validator = WalkForwardValidator(
        strategy_factory,
        parameter_grid,
        engine_factory,
        WalkForwardConfig(
            train_bars=_preset_value(args, "train_bars"),
            test_bars=_preset_value(args, "test_bars"),
            step_bars=_preset_value(args, "step_bars"),
            purge_bars=_preset_value(args, "purge_bars"),
            embargo_bars=_preset_value(args, "embargo_bars"),
            max_parameter_combinations=_preset_value(args, "max_params"),
        ),
    )
    result = validator.run(data)
    summary = result.summary()
    if args.output_summary:
        summary.to_csv(args.output_summary, index=False)
    print(summary.to_json(orient="records", indent=2, force_ascii=False, date_format="iso"))
    return 0


def _run_research_pack(args: argparse.Namespace) -> int:
    written = write_research_pack(args.output_dir, args.source_report)
    print(
        json.dumps(
            {key: str(path) for key, path in written.items()},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _run_qa_data(args: argparse.Namespace) -> int:
    data = filter_price_data_by_date(load_price_csvs(args.data), args.start_date, args.end_date)
    report = validate_price_data(
        data,
        DataQualityConfig(
            expected_frequency=args.expected_frequency,
            max_missing_pct=args.max_missing_pct,
        ),
    )
    if args.output:
        report.to_csv(args.output, index=False)
    print(report.to_json(orient="records", indent=2, force_ascii=False, date_format="iso"))
    return 0 if bool(report["passed"].all()) else 1


def _run_audit_run(args: argparse.Namespace) -> int:
    report = audit_run_artifacts(args.run_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


def _run_analyze_run(args: argparse.Namespace) -> int:
    ruin_threshold = (
        args.ruin_threshold_pct / 100 if args.ruin_threshold_pct > 1 else args.ruin_threshold_pct
    )
    cost_multipliers = (
        tuple(float(value) for value in args.cost_multiplier)
        if args.cost_multiplier
        else RunAnalysisConfig().cost_multipliers
    )
    written = analyze_run_artifacts(
        args.run_dir,
        output_dir=args.output_dir,
        config=RunAnalysisConfig(
            oos_ratio=args.oos_ratio,
            monte_carlo_paths=args.monte_carlo_paths,
            monte_carlo_seed=args.monte_carlo_seed,
            ruin_threshold_pct=ruin_threshold,
            cost_multipliers=cost_multipliers,
            min_period_days=args.min_period_days,
            min_oos_trades=args.min_oos_trades,
            min_walk_forward_folds=args.min_walk_forward_folds,
            min_forward_days=args.min_forward_days,
            monthly_target_return_pct=_pct_value(args.monthly_target_return),
        ),
        walk_forward_summary_path=args.walk_forward_summary,
        forward_trades_path=args.forward_trades,
        write_html=args.write_html,
    )
    print(
        json.dumps(
            {key: str(path) for key, path in written.items()},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _run_tradingview_webhook(args: argparse.Namespace) -> int:
    secret = _webhook_secret(args)
    config = TradingViewWebhookConfig(
        host=args.host,
        port=args.port,
        path=args.path,
        output_path=Path(args.output),
        secret=secret,
        max_body_bytes=args.max_body_bytes,
    )
    print(
        json.dumps(
            {
                "status": "listening",
                "url": f"http://{config.host}:{config.port}{config.path}",
                "health_url": f"http://{config.host}:{config.port}/health",
                "output": str(config.output_path),
                "secret_required": secret is not None,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return run_tradingview_webhook_server(config)


def _build_config(args: argparse.Namespace) -> BacktestConfig:
    spread_items = _preset_value(args, "spread_pips")
    slippage_items = _preset_value(args, "slippage_pips")
    spread_time_items = _preset_value(args, "spread_time_multiplier")
    slippage_time_items = _preset_value(args, "slippage_time_multiplier")
    execution = ExecutionConfig(
        spread_pips=_symbol_float_overrides(spread_items, ExecutionConfig().spread_pips),
        slippage_pips=_symbol_float_overrides(slippage_items, ExecutionConfig().slippage_pips),
        spread_time_multipliers=_int_float_overrides(
            spread_time_items,
            {},
        ),
        slippage_time_multipliers=_int_float_overrides(
            slippage_time_items,
            {},
        ),
        commission_per_million_usd=_preset_value(args, "commission_per_million"),
        fixed_fee_usd=_preset_value(args, "fixed_fee"),
        minimum_fee_usd=_preset_value(args, "minimum_fee"),
    )
    risk = RiskConfig(
        risk_per_trade_pct=_preset_value(args, "risk_per_trade"),
        risk_cap_pct=_preset_value(args, "risk_cap"),
        max_daily_loss_pct=_preset_value(args, "max_daily_loss"),
        max_weekly_loss_pct=_preset_value(args, "max_weekly_loss"),
        max_monthly_drawdown_pct=_preset_value(args, "max_monthly_drawdown"),
        monthly_profit_target_pct=_optional_pct_value(_preset_value(args, "monthly_profit_target")),
        hard_drawdown_pct=_preset_value(args, "hard_drawdown"),
        min_stop_pips=_preset_value(args, "min_stop_pips"),
        max_leverage=_preset_value(args, "max_leverage"),
        max_currency_exposure_pct=_preset_value(args, "max_currency_exposure"),
        max_position_units=_preset_value(args, "max_position_units"),
        allow_fractional_units=_preset_value(args, "allow_fractional_units"),
    )
    return BacktestConfig(
        initial_cash=_preset_value(args, "initial_cash"),
        risk=risk,
        execution=execution,
        no_trade_minutes_before=_preset_value(args, "no_trade_before"),
        no_trade_minutes_after=_preset_value(args, "no_trade_after"),
        min_event_impact=_preset_value(args, "min_event_impact"),
        max_open_positions=_preset_value(args, "max_open_positions"),
        cooldown_bars_after_stop=_preset_value(args, "cooldown_bars_after_stop"),
        trading_start_time=_preset_value(args, "trading_start"),
        trading_end_time=_preset_value(args, "trading_end"),
        blocked_weekdays=_parse_weekdays(_preset_value(args, "blocked_weekday")),
        conversion_rates=_symbol_float_overrides(_preset_value(args, "conversion_rate"), {}),
        close_positions_on_daily_stop=_preset_value(args, "close_positions_on_daily_stop"),
        close_positions_on_portfolio_stop=_preset_value(args, "close_positions_on_portfolio_stop"),
        force_close_on_end=_preset_value(args, "force_close_on_end"),
    )


def _preset_value(args: argparse.Namespace, key: str) -> Any:
    value = getattr(args, key)
    default = DEFAULT_CLI_VALUES[key]
    preset_name = getattr(args, "preset", None)
    if value is None:
        if preset_name is None:
            return default
        return PRESETS[preset_name].get(key, default)
    if preset_name is None:
        return value
    if value == default:
        return PRESETS[preset_name].get(key, value)
    return value


def _webhook_secret(args: argparse.Namespace) -> str | None:
    if args.secret and args.secret_env:
        raise ValueError("Use either --secret or --secret-env, not both")
    if args.secret_env:
        secret = os.environ.get(args.secret_env)
        if not secret:
            raise ValueError(f"Environment variable {args.secret_env!r} is not set")
        return secret
    return args.secret


def _optional_pct_value(value: float | None) -> float | None:
    if value is None:
        return None
    return _pct_value(value)


def _pct_value(value: float) -> float:
    numeric = float(value)
    return numeric / 100 if numeric > 1 else numeric


def _build_strategy(name: str, param_items: list[str] | None) -> Any:
    return STRATEGY_REGISTRY[name](**_build_strategy_params(param_items))


def _build_strategy_from_args(args: argparse.Namespace, params: dict[str, Any]) -> Any:
    strategy = STRATEGY_REGISTRY[args.strategy](**params)
    if not (_preset_value(args, "regime_filter") or _preset_value(args, "signal_no_trade_filter")):
        return strategy
    return FilteredStrategy(
        strategy,
        RegimeFilterConfig(
            enabled=bool(_preset_value(args, "regime_filter")),
            window=int(_preset_value(args, "regime_window")),
            slope_window=int(_preset_value(args, "regime_slope_window")),
            min_atr_percentile=float(_preset_value(args, "regime_min_atr_percentile")),
            max_atr_percentile=float(_preset_value(args, "regime_max_atr_percentile")),
        ),
        NoTradeFilterConfig(
            enabled=bool(_preset_value(args, "signal_no_trade_filter")),
            blocked_entry_hours=_parse_hours(_preset_value(args, "blocked_entry_hour")),
            max_spread_multiple=float(_preset_value(args, "max_spread_multiple")),
            spread_lookback=int(_preset_value(args, "spread_lookback")),
        ),
    )


def _strategy_factory_from_args(args: argparse.Namespace) -> Any:
    def factory(**params: Any) -> Any:
        return _build_strategy_from_args(args, params)

    return factory


def _build_strategy_params(param_items: list[str] | None) -> dict[str, Any]:
    param_items = param_items or []
    return {
        key: _cast_value(value) for key, value in (_split_key_value(item) for item in param_items)
    }


def _build_grid(strategy_name: str, grid_items: list[str] | None) -> dict[str, list[object]]:
    if not grid_items:
        return DEFAULT_PARAM_GRIDS[strategy_name]
    grid: dict[str, list[object]] = {}
    for item in grid_items:
        key, value = _split_key_value(item)
        grid[key] = [_cast_value(part) for part in value.split(",")]
    return grid


def _symbol_float_overrides(
    items: list[str] | None,
    defaults: dict[str, float],
) -> dict[str, float]:
    output = defaults.copy()
    for item in items or []:
        key, value = _split_key_value(item)
        output[key.upper().replace("/", "")] = float(value)
    return output


def _int_float_overrides(items: list[str] | None, defaults: dict[int, float]) -> dict[int, float]:
    output = defaults.copy()
    for item in items or []:
        key, value = _split_key_value(item)
        int_key = int(key)
        if int_key < 0 or int_key > 23:
            raise ValueError(f"Hour must be 0-23, got {int_key}")
        output[int_key] = float(value)
    return output


def _split_key_value(item: str) -> tuple[str, str]:
    if "=" not in item:
        raise ValueError(f"Expected key=value, got {item!r}")
    key, value = item.split("=", 1)
    return key.strip(), value.strip()


def _parse_weekdays(items: list[str] | None) -> tuple[int, ...]:
    names = {
        "mon": 0,
        "monday": 0,
        "tue": 1,
        "tuesday": 1,
        "wed": 2,
        "wednesday": 2,
        "thu": 3,
        "thursday": 3,
        "fri": 4,
        "friday": 4,
        "sat": 5,
        "saturday": 5,
        "sun": 6,
        "sunday": 6,
    }
    weekdays: set[int] = set()
    for item in items or []:
        for raw_part in item.split(","):
            part = raw_part.strip().lower()
            if not part:
                continue
            value = names.get(part)
            if value is None:
                try:
                    value = int(part)
                except ValueError as error:
                    raise ValueError(
                        f"Expected weekday 0-6 or mon-sun, got {raw_part!r}"
                    ) from error
            if value < 0 or value > 6:
                raise ValueError(f"Weekday must be 0-6, got {value}")
            weekdays.add(value)
    return tuple(sorted(weekdays))


def _parse_hours(items: list[str] | None) -> tuple[int, ...]:
    hours: set[int] = set()
    for item in items or []:
        for raw_part in item.split(","):
            part = raw_part.strip()
            if not part:
                continue
            value = int(part)
            if value < 0 or value > 23:
                raise ValueError(f"Hour must be 0-23, got {value}")
            hours.add(value)
    return tuple(sorted(hours))


def _cast_value(value: str) -> object:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if any(character in value for character in (".", "e", "E")):
            return float(value)
        return int(value)
    except ValueError:
        return value


def _jsonable(metrics: dict[str, float | int]) -> dict[str, float | int | str]:
    output: dict[str, float | int | str] = {}
    for key, value in metrics.items():
        if isinstance(value, float) and value == float("inf"):
            output[key] = "inf"
        else:
            output[key] = value
    return output


if __name__ == "__main__":
    raise SystemExit(main())
