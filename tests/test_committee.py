"""複数AI委員会(committee.py)のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import date, datetime, UTC

from fx_intel.committee import deliberate, macro_opinion, ml_opinion
from fx_intel.macro import CotReport, MacroSnapshot
from fx_intel.ml import MLArtifact
from fx_intel.sentiment import CurrencySentiment
from fx_intel.technicals import IntervalView, PairTechnicals

NOW = datetime(2026, 7, 2, 8, 0, tzinfo=UTC)  # 木曜・市場オープン


def _tech() -> PairTechnicals:
    tech = PairTechnicals(symbol="USDJPY", fast_window=20, slow_window=100)
    tech.views["1h"] = IntervalView(
        interval="1h",
        recommendation="BUY",
        buy=10,
        sell=2,
        neutral=3,
        close=155.0,
        rsi=58.0,
        atr=0.3,
        sma_fast=155.1,
        sma_slow=154.5,
    )
    tech.views["4h"] = IntervalView(
        interval="4h", recommendation="BUY", buy=8, sell=1, neutral=2, close=155.0
    )
    return tech


def _scores() -> dict[str, CurrencySentiment]:
    return {
        "USD": CurrencySentiment("USD", score=0.3),
        "JPY": CurrencySentiment("JPY", score=-0.2),
    }


def _macro_snapshot() -> MacroSnapshot:
    snap = MacroSnapshot(fetched_at=NOW)
    snap.cot["USD"] = CotReport("USD", date(2026, 6, 30), net_position=50000, open_interest=200000)
    snap.cot["JPY"] = CotReport("JPY", date(2026, 6, 30), net_position=-40000, open_interest=200000)
    return snap


def test_macro_opinion_shadow_is_inactive() -> None:
    opinion = macro_opinion("USDJPY", _macro_snapshot(), stage="shadow")
    assert opinion is not None
    assert not opinion.active
    assert opinion.stage == "shadow"


def test_macro_opinion_paper_is_active() -> None:
    opinion = macro_opinion("USDJPY", _macro_snapshot(), stage="paper")
    assert opinion is not None
    assert opinion.active


def test_ml_opinion_none_when_unusable() -> None:
    assert ml_opinion(MLArtifact(), 0.5, 0.3, {}, stage="paper") is None


def test_shadow_member_recorded_but_not_in_composite() -> None:
    plan = deliberate(
        "USDJPY",
        _tech(),
        _scores(),
        [],
        [],
        now=NOW,
        macro_snapshot=_macro_snapshot(),
        stages={"macro": "shadow"},
    )
    component_keys = [c["key"] for c in plan.components]
    assert "macro" not in component_keys  # 合成に参加しない
    assert plan.features.get("macro_score") is not None  # だが記録はされる
    assert any("shadow" in note for note in plan.committee_notes)


def test_paper_member_joins_composite() -> None:
    shadow = deliberate(
        "USDJPY",
        _tech(),
        _scores(),
        [],
        [],
        now=NOW,
        macro_snapshot=_macro_snapshot(),
        stages={"macro": "shadow"},
    )
    paper = deliberate(
        "USDJPY",
        _tech(),
        _scores(),
        [],
        [],
        now=NOW,
        macro_snapshot=_macro_snapshot(),
        stages={"macro": "paper"},
    )
    assert "macro" in [c["key"] for c in paper.components]
    # マクロがUSD/JPYロング方向なので、参加すると複合スコアが上がる
    assert paper.composite >= shadow.composite


def test_deliberate_without_extras_matches_build_trade_plan() -> None:
    """追加委員が無ければ従来のbuild_trade_planと同じ結果になる(後方互換)。"""
    from fx_intel.briefing import build_trade_plan

    plan_committee = deliberate("USDJPY", _tech(), _scores(), [], [], now=NOW)
    plan_direct = build_trade_plan("USDJPY", _tech(), _scores(), [], [], now=NOW)
    assert plan_committee.composite == plan_direct.composite
    assert plan_committee.direction == plan_direct.direction
    assert plan_committee.conviction == plan_direct.conviction


def test_risk_officer_gate_still_applies_over_committee() -> None:
    """委員会があっても休場中は方向判断が closed に固定される(リスクオフィサー拒否権)。"""
    weekend = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)  # 土曜
    plan = deliberate(
        "USDJPY",
        _tech(),
        _scores(),
        [],
        [],
        now=weekend,
        macro_snapshot=_macro_snapshot(),
        stages={"macro": "paper"},
    )
    assert plan.direction == "closed"
    assert plan.conviction == 0
