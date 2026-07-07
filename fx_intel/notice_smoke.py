"""End-to-end smoke test for the detailed notice pipeline.

The smoke path is deterministic and offline.  It exercises the operational
pipeline without fetching live market data or posting to Discord:

1. build a detailed notice from synthetic but realistic inputs
2. render and split the Discord text payloads
3. append/read the detailed notice journal
4. score the journal against future OHLC bars
5. write JSON/CSV quality reports
6. save a feedback profile
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from pathlib import Path

from . import (
    discord_delivery,
    notice_feedback,
    notice_journal,
    notice_quality,
    notice_renderer,
    trade_notice,
)
from .briefing import TradePlan
from .calendar import EconomicEvent
from .market_structure import EntryLevels, OhlcBar
from .sentiment import CurrencySentiment, MarketAnalysis
from .technicals import PairTechnicals, build_interval_view

SMOKE_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class NoticePipelineSmokeResult:
    output_dir: Path
    journal_path: Path
    report_markdown_path: Path
    quality_json_path: Path
    quality_csv_path: Path
    feedback_path: Path
    chunk_count: int
    outcome: str
    entry_scenario: str
    summary_text: str


def run_notice_pipeline_smoke(
    output_dir: str | Path,
    *,
    now: datetime = SMOKE_NOW,
) -> NoticePipelineSmokeResult:
    """Run the offline detailed-notice smoke pipeline and write artifacts."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    notice, levels = _build_notice(now)
    report_text = notice_renderer.render_notices_markdown([notice])
    payloads = discord_delivery.build_discord_text_payloads(report_text)

    report_path = output / "trade_notice_report.md"
    journal_path = output / "trade_notice_journal.jsonl"
    quality_json_path = output / "notice_quality.json"
    quality_csv_path = output / "notice_quality.csv"
    feedback_path = output / "trade_notice_feedback.json"

    report_path.write_text(report_text, encoding="utf-8")
    notice_journal.append_detailed_notices(
        journal_path,
        [notice],
        report_text=report_text,
        entry_levels_by_symbol={notice.symbol: levels},
        chunk_count=len(payloads),
        delivery="smoke",
        now=now,
    )

    entries = list(notice_journal.read_notice_entries(journal_path))
    outcomes = notice_quality.score_notice_entries(
        entries,
        {notice.symbol: _future_bars(now)},
    )
    summary = notice_quality.summarize_outcomes(outcomes)
    notice_quality.write_quality_report_json(
        quality_json_path,
        entries,
        outcomes,
        generated_at=now,
    )
    notice_quality.write_quality_outcomes_csv(quality_csv_path, entries, outcomes)
    profile = notice_feedback.build_feedback_profile(entries, outcomes, now=now)
    notice_feedback.save_profile(profile, feedback_path)

    outcome = (
        outcomes[0] if outcomes else notice_quality.NoticeQualityOutcome("", None, "", "missing")
    )
    return NoticePipelineSmokeResult(
        output_dir=output,
        journal_path=journal_path,
        report_markdown_path=report_path,
        quality_json_path=quality_json_path,
        quality_csv_path=quality_csv_path,
        feedback_path=feedback_path,
        chunk_count=len(payloads),
        outcome=outcome.outcome,
        entry_scenario=outcome.entry_scenario,
        summary_text=notice_quality.format_summary_ja(summary),
    )


def _build_notice(now: datetime):
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        "15m": _view("15m", "BUY"),
        "1h": _view("1h", "BUY"),
        "4h": _view("4h", "STRONG_BUY"),
        "1d": _view("1d", "STRONG_BUY"),
    }
    plan = TradePlan(
        symbol="USDJPY",
        direction="long",
        conviction=52,
        composite=0.52,
        tech_score=0.55,
        news_score=0.10,
        close=162.296,
        atr=0.153,
        stop=161.914,
        target1=162.678,
        target2=163.060,
        data_quality=0.8,
        interval_summary="15m 買い | 1h 買い | 4h 強い買い | 1d 強い買い",
        ma_note="移動平均線MA(20/100): デッドクロス(売りが優勢のサイン)",
    )
    analysis = MarketAnalysis(
        engine="smoke",
        regime="neutral",
        currencies={
            "USD": CurrencySentiment("USD", score=0.2, headline_count=3),
            "JPY": CurrencySentiment("JPY", score=-0.1, headline_count=2),
        },
    )
    events = [EconomicEvent("ISM Services PMI", "USD", now + timedelta(hours=2), "high")]
    levels = EntryLevels(
        "USDJPY",
        "long",
        162.20,
        162.30,
        162.35,
        162.45,
        162.20,
        162.45,
        162.10,
        162.50,
        "recent_ohlc",
        48,
    )
    notice = trade_notice.build_detailed_notice(
        plan,
        tech,
        analysis,
        events,
        now=now,
        entry_levels=levels,
    )
    return notice, levels


def _view(interval: str, recommendation: str):
    summary = {"RECOMMENDATION": recommendation, "BUY": 12, "SELL": 5, "NEUTRAL": 9}
    indicators = {
        "close": 162.296,
        "RSI": 57.0,
        "ADX": 24.0,
        "ATR": 0.153,
        "SMA20": 162.20,
        "SMA100": 162.40,
    }
    return build_interval_view(interval, summary, indicators, 20, 100)


def _future_bars(now: datetime) -> list[OhlcBar]:
    return [
        OhlcBar(
            timestamp=now + timedelta(minutes=15),
            open=162.30,
            high=162.34,
            low=162.22,
            close=162.28,
        ),
        OhlcBar(
            timestamp=now + timedelta(minutes=30),
            open=162.28,
            high=162.42,
            low=162.26,
            close=162.36,
        ),
        OhlcBar(
            timestamp=now + timedelta(minutes=45),
            open=162.36,
            high=162.70,
            low=162.40,
            close=162.65,
        ),
    ]
