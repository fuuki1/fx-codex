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


def test_legacy_paper_stage_fails_closed_to_shadow() -> None:
    opinion = macro_opinion("USDJPY", _macro_snapshot(), stage="paper")
    assert opinion is not None
    assert opinion.stage == "shadow"
    assert not opinion.active


def test_legacy_live_stage_fails_closed_to_shadow() -> None:
    opinion = macro_opinion("USDJPY", _macro_snapshot(), stage="live")
    assert opinion is not None
    assert opinion.stage == "shadow"
    assert not opinion.active


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


def test_legacy_paper_member_cannot_join_composite() -> None:
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
    assert "macro" not in [c["key"] for c in paper.components]
    assert paper.composite == shadow.composite


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


def _usable_artifact_with_return_heads() -> MLArtifact:
    """最小構成: usableな二値ヘッド + 収益ヘッド(回帰+分位点)を持つ artifact。"""
    import random

    from fx_intel.gbm import (
        CalibrationResult,
        GradientBoostingClassifier,
        GradientBoostingRegressor,
    )
    from fx_intel.ml import FEATURE_NAMES

    rng = random.Random(0)
    features = [[rng.gauss(0, 1) for _ in range(len(FEATURE_NAMES))] for _ in range(60)]
    binary = [1 if row[0] > 0 else 0 for row in features]
    returns = [0.5 * row[0] + rng.gauss(0, 0.2) for row in features]

    art = MLArtifact()
    art.model = GradientBoostingClassifier(n_estimators=30).fit(features, binary)
    art.usable = True
    art.medians = {name: 0.0 for name in FEATURE_NAMES}
    art.calibration = CalibrationResult(scale=1.0, offset=0.0)
    art.return_model = GradientBoostingRegressor(n_estimators=30).fit(features, returns)
    art.return_usable = True
    for name, q in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9)):
        art.quantile_models[name] = GradientBoostingRegressor(
            objective="quantile", quantile=q, n_estimators=20
        ).fit(features, returns)
    return art


def test_ml_opinion_shows_return_head_as_shadow_note() -> None:
    """収益ヘッドが usable なら期待純R・分位点帯を rationale に載せる(scoreは不変)。"""
    art = _usable_artifact_with_return_heads()
    opinion = ml_opinion(art, 0.6, 0.2, {"rsi_1h": 65.0, "adx_1h": 25.0})
    assert opinion is not None
    # 二値の優位差がそのまま score(収益ヘッドは score に影響しない)
    p_long, p_short = art.direction_edge(0.6, 0.2, {"rsi_1h": 65.0, "adx_1h": 25.0})
    assert opinion.score == round(p_long - p_short, 3)
    # 期待純R・分位点帯の shadow 行が含まれる
    assert any("期待純R" in line for line in opinion.rationale_ja)
    assert any("純R帯" in line for line in opinion.rationale_ja)


def test_ml_opinion_omits_return_note_when_return_head_unusable() -> None:
    """収益ヘッドが usable でなければ純R行は出ない(二値の意見だけ)。"""
    art = _usable_artifact_with_return_heads()
    art.return_usable = False  # 収益ヘッドを非採用に
    opinion = ml_opinion(art, 0.6, 0.2, {"rsi_1h": 65.0, "adx_1h": 25.0})
    assert opinion is not None
    assert not any("期待純R" in line for line in opinion.rationale_ja)
