from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pandas as pd


RESEARCH_MAX_PRESET: dict[str, Any] = {
    "initial_cash": 100_000.0,
    "risk_per_trade": 0.005,
    "risk_cap": 0.01,
    "max_daily_loss": 0.015,
    "min_stop_pips": 7.0,
    "max_leverage": 5.0,
    "max_currency_exposure": 2.0,
    "max_position_units": 250_000.0,
    "allow_fractional_units": False,
    "max_open_positions": 2,
    "cooldown_bars_after_stop": 3,
    "commission_per_million": 35.0,
    "fixed_fee": 0.0,
    "minimum_fee": 0.0,
    "spread_pips": [
        "EURUSD=0.8",
        "GBPUSD=1.1",
        "USDJPY=1.0",
        "USDCHF=1.4",
        "AUDUSD=1.0",
        "USDCAD=1.3",
        "NZDUSD=1.2",
    ],
    "slippage_pips": [
        "EURUSD=0.2",
        "GBPUSD=0.3",
        "USDJPY=0.25",
        "USDCHF=0.35",
        "AUDUSD=0.25",
        "USDCAD=0.3",
        "NZDUSD=0.35",
    ],
    "spread_time_multiplier": ["21=2.0", "22=1.5", "0=1.25"],
    "slippage_time_multiplier": ["21=2.0", "22=1.5", "0=1.25"],
    "no_trade_before": 120,
    "no_trade_after": 180,
    "min_event_impact": "medium",
    "trading_start": "06:00",
    "trading_end": "22:00",
    "blocked_weekday": ["sat", "sun"],
    "close_positions_on_daily_stop": True,
    "force_close_on_end": True,
    "train_bars": 1_500,
    "test_bars": 300,
    "step_bars": 150,
    "purge_bars": 24,
    "embargo_bars": 12,
    "max_params": 24,
}


DEEP_RESEARCH_MAX_PRESET: dict[str, Any] = {
    "initial_cash": 100_000.0,
    "risk_per_trade": 0.005,
    "risk_cap": 0.005,
    "max_daily_loss": 0.015,
    "max_weekly_loss": 0.03,
    "max_monthly_drawdown": 0.06,
    "hard_drawdown": 0.10,
    "min_stop_pips": 8.0,
    "max_leverage": 5.0,
    "max_currency_exposure": 2.0,
    "max_position_units": 250_000.0,
    "allow_fractional_units": False,
    "max_open_positions": 2,
    "cooldown_bars_after_stop": 4,
    "commission_per_million": 35.0,
    "fixed_fee": 0.0,
    "minimum_fee": 0.0,
    "spread_pips": [
        "USDJPY=1.0",
        "EURUSD=0.8",
        "GBPUSD=1.2",
        "AUDUSD=1.0",
        "USDCHF=1.4",
        "USDCAD=1.3",
        "EURJPY=1.4",
        "GBPJPY=1.8",
        "AUDJPY=1.5",
    ],
    "slippage_pips": [
        "USDJPY=0.25",
        "EURUSD=0.2",
        "GBPUSD=0.35",
        "AUDUSD=0.25",
        "USDCHF=0.35",
        "USDCAD=0.3",
        "EURJPY=0.35",
        "GBPJPY=0.5",
        "AUDJPY=0.4",
    ],
    "spread_time_multiplier": ["21=2.0", "22=1.5", "0=1.25"],
    "slippage_time_multiplier": ["21=2.0", "22=1.5", "0=1.25"],
    "no_trade_before": 180,
    "no_trade_after": 240,
    "min_event_impact": "medium",
    "trading_start": "06:00",
    "trading_end": "23:00",
    "blocked_weekday": ["sat", "sun"],
    "close_positions_on_daily_stop": True,
    "close_positions_on_portfolio_stop": True,
    "force_close_on_end": True,
    "train_bars": 2_000,
    "test_bars": 400,
    "step_bars": 200,
    "purge_bars": 24,
    "embargo_bars": 12,
    "max_params": 24,
}


PRESETS: dict[str, dict[str, Any]] = {
    "research-max": RESEARCH_MAX_PRESET,
    "deep-research-max": DEEP_RESEARCH_MAX_PRESET,
}


DEEP_RESEARCH_DECISIONS: list[dict[str, str]] = [
    {
        "area": "market_universe",
        "decision": "G10 core, starting with USDJPY, EURUSD, GBPUSD, AUDUSD, USDCHF, USDCAD",
        "reason": "BIS liquidity structure and lower implementation noise than minors/emerging-market FX",
    },
    {
        "area": "style",
        "decision": "day-trade to swing horizon; avoid scalping, retail market making, and pure arbitrage in V1",
        "reason": "public API latency and realistic transaction costs weaken ultra-short-horizon edges",
    },
    {
        "area": "risk",
        "decision": "0.25-0.50% NAV per trade, 1.5% daily stop, 3% weekly stop, 6% monthly soft DD, 10% hard DD",
        "reason": "internal risk limits should stop before broker margin and forced liquidation mechanics",
    },
    {
        "area": "validation",
        "decision": "data QA, IS/OOS, walk-forward, PBO/DSR review, paper, then micro-live",
        "reason": "selection bias and live/backtest execution gaps dominate early FX bot failures",
    },
    {
        "area": "operations",
        "decision": "automatic restart must not mean automatic trading restart",
        "reason": "position reconciliation and human-readable stop reasons are mandatory for incident recovery",
    },
]


PUBLIC_FX_SOURCES: list[dict[str, str]] = [
    {
        "name": "Federal Reserve H.10",
        "url": "https://www.federalreserve.gov/releases/h10/",
        "data_type": "official daily FX rates and USD indexes",
        "granularity": "daily",
        "use_case": "long-horizon regime checks and daily benchmark validation",
        "caveat": "daily official rates are not intraday tradable quotes",
    },
    {
        "name": "Dukascopy Historical Data Export",
        "url": "https://www.dukascopy.com/swiss/english/marketwatch/historical/",
        "data_type": "broker historical prices",
        "granularity": "tick to monthly CSV",
        "use_case": "intraday backtests with realistic bar construction",
        "caveat": "broker-specific history; validate timezone and bid/ask handling",
    },
    {
        "name": "HistData",
        "url": "https://www.histdata.com/download-free-forex-data/",
        "data_type": "free FX M1 and tick files",
        "granularity": "M1, tick/1-second variants by format",
        "use_case": "quick public-data replication and strategy smoke tests",
        "caveat": "check file specification, update cadence, and import conventions",
    },
    {
        "name": "BIS Triennial Central Bank Survey 2025",
        "url": "https://www.bis.org/statistics/rpfx25.htm",
        "data_type": "global OTC FX turnover and structure",
        "granularity": "triennial survey tables",
        "use_case": "currency universe, liquidity assumptions, and market-structure notes",
        "caveat": "survey data is market structure, not price history",
    },
    {
        "name": "SNB press releases",
        "url": "https://www.snb.ch/en/publications/communication/press-releases/2015/pre_20150115",
        "data_type": "central-bank shock event",
        "granularity": "event timestamp/date",
        "use_case": "CHF stress label and no-trade/stress-test windows",
        "caveat": "event label must be aligned to the quote data timezone",
    },
    {
        "name": "Bank of England market operation releases",
        "url": "https://www.bankofengland.co.uk/news/2022/september/bank-of-england-announces-gilt-market-operation",
        "data_type": "financial-stability policy event",
        "granularity": "event timestamp/date",
        "use_case": "GBP stress label and policy-intervention windows",
        "caveat": "FX reaction can begin before the official release via related market repricing",
    },
]


MAJOR_FX_EVENTS: list[dict[str, str]] = [
    {
        "timestamp": "2015-01-15 09:30:00",
        "currency": "CHF",
        "symbol": "",
        "impact": "high",
        "name": "SNB removes EURCHF minimum exchange rate",
        "category": "central_bank_shock",
        "source_url": "https://www.snb.ch/en/publications/communication/press-releases/2015/pre_20150115",
        "notes": "Use as a hard stress window for CHF and correlated EUR risk.",
    },
    {
        "timestamp": "2016-06-24 00:00:00",
        "currency": "GBP",
        "symbol": "",
        "impact": "high",
        "name": "UK Brexit referendum result",
        "category": "political_shock",
        "source_url": "https://www.bankofengland.co.uk/",
        "notes": "GBP liquidity and gap-risk stress label around the vote count.",
    },
    {
        "timestamp": "2020-03-16 00:00:00",
        "currency": "USD",
        "symbol": "",
        "impact": "high",
        "name": "COVID USD funding stress window",
        "category": "liquidity_shock",
        "source_url": "https://www.federalreserve.gov/",
        "notes": "Treat March 2020 as a multi-day volatility and liquidity stress period.",
    },
    {
        "timestamp": "2022-09-28 00:00:00",
        "currency": "GBP",
        "symbol": "",
        "impact": "high",
        "name": "Bank of England temporary long-dated gilt purchases",
        "category": "financial_stability_intervention",
        "source_url": "https://www.bankofengland.co.uk/news/2022/september/bank-of-england-announces-gilt-market-operation",
        "notes": "GBP stress label after UK mini-budget and gilt market dysfunction.",
    },
    {
        "timestamp": "2022-10-21 00:00:00",
        "currency": "JPY",
        "symbol": "USDJPY",
        "impact": "high",
        "name": "Japan yen intervention stress window",
        "category": "fx_intervention",
        "source_url": "https://www.mof.go.jp/english/policy/international_policy/reference/feio/index.htm",
        "notes": "Use as USDJPY intervention-risk stress window.",
    },
]


def public_sources_frame() -> pd.DataFrame:
    return pd.DataFrame(PUBLIC_FX_SOURCES)


def major_events_frame() -> pd.DataFrame:
    return pd.DataFrame(MAJOR_FX_EVENTS)


def research_max_config() -> dict[str, Any]:
    return {
        "name": "research-max",
        "description": (
            "Conservative public-FX-history preset built around official daily data, "
            "public intraday broker histories, major central-bank shocks, and "
            "walk-forward validation."
        ),
        "cli_preset": RESEARCH_MAX_PRESET,
        "data_plan": {
            "preferred_intraday": "Dukascopy tick or M1 data exported to timestamp,symbol,open,high,low,close",
            "replication_intraday": "HistData Generic ASCII M1 or tick files converted to the project CSV schema",
            "daily_benchmark": "Federal Reserve H.10 daily rates",
            "market_structure": "BIS Triennial Central Bank Survey 2025 final tables",
        },
        "analysis_rules": [
            "Never treat public OHLC quotes as personal execution history.",
            "Run normal-period and stress-period metrics separately.",
            "Keep parameter grids intentionally small and walk-forward only.",
            "Block new entries around high-impact events; still execute exits and stops.",
            "Include spread, slippage, commission, leverage, and position caps in sizing.",
        ],
        "stress_events": MAJOR_FX_EVENTS,
        "sources": PUBLIC_FX_SOURCES,
    }


def deep_research_max_config(source_report: str | Path | None = None) -> dict[str, Any]:
    return {
        "name": "deep-research-max",
        "description": (
            "Maximal execution preset derived from the local deep research report: "
            "G10 core FX, day/swing horizon, conservative internal leverage, "
            "portfolio stops, strict event filters, data QA, and staged validation."
        ),
        "source_report": str(source_report) if source_report else "",
        "cli_preset": DEEP_RESEARCH_MAX_PRESET,
        "decisions": DEEP_RESEARCH_DECISIONS,
        "validation_gates": [
            "data_qa",
            "research_backtest",
            "is_oos_split",
            "walk_forward",
            "pbo_dsr_review",
            "paper_trading",
            "micro_live",
        ],
        "go_live_rule": (
            "Go-live is not a profitable backtest; it requires manageable paper/micro-live "
            "slippage, reconnect behavior, logs, and designed-loss behavior."
        ),
        "stress_events": MAJOR_FX_EVENTS,
        "sources": PUBLIC_FX_SOURCES,
    }


def write_research_pack(
    output_dir: str | Path,
    source_report: str | Path | None = None,
) -> dict[str, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    sources_path = destination / "public_fx_sources.csv"
    events_path = destination / "major_fx_events.csv"
    config_path = destination / "research_max_config.json"
    deep_config_path = destination / "deep_research_max_config.json"
    decisions_path = destination / "deep_research_decisions.csv"
    notes_path = destination / "research_notes.md"

    public_sources_frame().to_csv(sources_path, index=False)
    major_events_frame().to_csv(events_path, index=False)
    pd.DataFrame(DEEP_RESEARCH_DECISIONS).to_csv(decisions_path, index=False)
    config_path.write_text(
        json.dumps(research_max_config(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    deep_config_path.write_text(
        json.dumps(deep_research_max_config(source_report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    notes_path.write_text(_research_notes_markdown(), encoding="utf-8")

    written = {
        "sources": sources_path,
        "events": events_path,
        "config": config_path,
        "deep_config": deep_config_path,
        "deep_decisions": decisions_path,
        "notes": notes_path,
    }

    if source_report:
        source = Path(source_report)
        if source.exists():
            copied_report_path = destination / "source_deep_research_report.md"
            shutil.copyfile(source, copied_report_path)
            written["source_report"] = copied_report_path

    return written


def _research_notes_markdown() -> str:
    return """# FX Public History Research Pack

This pack is built for repeatable FX backtests using public market data.

Key interpretation:

- Public FX history is usually quote or bar history, not a trader's actual executions.
- OTC FX has no single consolidated public tape, so source metadata matters.
- Use official daily rates for benchmark checks and public broker/tick data for intraday tests.
- Label major policy shocks separately and compare normal-period metrics with stress-period metrics.
- Keep risk smaller than the default smoke-test setup because public historical quotes do not prove future fill quality.

Recommended workflow:

1. Export or convert price files to `timestamp,symbol,open,high,low,close`.
2. Run `qa-data` before trusting any imported history.
3. Run `backtest` with `--preset deep-research-max` and `--events major_fx_events.csv`.
4. Run `walk-forward` with the same preset and a small parameter grid.
5. Review `max_drawdown_pct`, `expectancy_r`, `profit_factor`, `calmar_ratio`, and `exposure_pct`.
6. Re-run the same strategy on stress windows before trusting aggregate metrics.
"""
