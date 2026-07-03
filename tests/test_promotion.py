"""昇格ゲート(promotion.py)のテスト(ネットワーク不要)。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, UTC

from fx_intel.promotion import (
    MemberPerformance,
    PromotionState,
    _one_sided_binomial_pvalue,
    evaluate_and_update,
    evaluate_member,
    load_state,
    save_state,
    update_stages,
)

NOW = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
START = datetime(2026, 1, 6, 0, 0, tzinfo=UTC)


def _journal_with_signal(n: int, hit_prob: float, seed: int) -> list[dict]:
    """ml_edgeの符号がhit_probの確率で将来値動きと一致する合成ジャーナル。"""
    rng = random.Random(seed)
    entries: list[dict] = []
    price = 155.0
    for i in range(n):
        ts = START + timedelta(hours=8 * i)
        edge = rng.choice([-0.4, -0.3, 0.3, 0.4])
        entries.append(
            {
                "ts": ts.isoformat(),
                "symbol": "USDJPY",
                "close": round(price, 3),
                "atr": 0.3,
                "features": {"ml_edge": edge},
            }
        )
        correct = rng.random() < hit_prob
        move = (0.5 if edge > 0 else -0.5) * (1 if correct else -1)
        price += move + rng.gauss(0, 0.05)
    entries.append(
        {
            "ts": (START + timedelta(hours=8 * n)).isoformat(),
            "symbol": "USDJPY",
            "close": round(price, 3),
            "atr": 0.3,
            "features": {},
        }
    )
    return entries


def test_binomial_pvalue_sanity() -> None:
    assert _one_sided_binomial_pvalue(60, 100) < 0.05  # 60/100は有意
    assert _one_sided_binomial_pvalue(50, 100) > 0.4  # 50/100は偶然の範囲
    assert _one_sided_binomial_pvalue(0, 0) == 1.0


def test_good_signal_promotes_shadow_to_paper() -> None:
    entries = _journal_with_signal(500, hit_prob=0.72, seed=1)
    perf = evaluate_member("ml", entries, now=NOW)
    ok, reasons = perf.meets_promotion()
    assert ok, reasons
    state = PromotionState()
    update_stages(state, {"ml": perf}, now=NOW)
    assert state.stage_of("ml") == "paper"


def test_weak_signal_stays_in_shadow() -> None:
    entries = _journal_with_signal(500, hit_prob=0.50, seed=2)
    perf = evaluate_member("ml", entries, now=NOW)
    assert not perf.meets_promotion()[0]
    state = PromotionState()
    update_stages(state, {"ml": perf}, now=NOW)
    assert state.stage_of("ml") == "shadow"


def test_degraded_paper_member_auto_demotes() -> None:
    bad = MemberPerformance(member="ml", evaluated=60, hits=25, expectancy_atr=-0.1, p_value=0.9)
    state = PromotionState(stages={"macro": "shadow", "ml": "paper"})
    update_stages(state, {"ml": bad}, now=NOW)
    assert state.stage_of("ml") == "shadow"


def test_live_requires_human_ack() -> None:
    good = MemberPerformance(member="ml", evaluated=100, hits=60, expectancy_atr=0.3, p_value=0.01)
    # 承認なしではpaperのまま
    state = PromotionState(stages={"macro": "shadow", "ml": "paper"})
    update_stages(state, {"ml": good}, now=NOW)
    assert state.stage_of("ml") == "paper"
    # 承認ありでliveへ
    update_stages(state, {"ml": good}, now=NOW, require_live_ack=["ml"])
    assert state.stage_of("ml") == "live"


def test_live_ack_without_meeting_criteria_holds() -> None:
    """承認があっても昇格条件未達なら live に上げない。"""
    weak = MemberPerformance(member="ml", evaluated=20, hits=11, expectancy_atr=0.0, p_value=0.5)
    state = PromotionState(stages={"macro": "shadow", "ml": "paper"})
    update_stages(state, {"ml": weak}, now=NOW, require_live_ack=["ml"])
    assert state.stage_of("ml") == "paper"


def test_state_roundtrip(tmp_path) -> None:
    entries = _journal_with_signal(500, hit_prob=0.72, seed=1)
    state = PromotionState()
    state, _ = evaluate_and_update(entries, state, now=NOW)
    path = tmp_path / "promotion.json"
    save_state(state, path)
    loaded = load_state(path)
    assert loaded.stages == state.stages


def test_load_unknown_stage_resets_to_shadow(tmp_path) -> None:
    path = tmp_path / "promotion.json"
    path.write_text('{"stages": {"ml": "bogus", "macro": "paper"}}', encoding="utf-8")
    loaded = load_state(path)
    assert loaded.stage_of("ml") == "shadow"
    assert loaded.stage_of("macro") == "paper"


def test_history_records_transitions() -> None:
    good = MemberPerformance(member="ml", evaluated=100, hits=60, expectancy_atr=0.3, p_value=0.01)
    state = PromotionState()
    update_stages(state, {"ml": good}, now=NOW)
    assert any(h["member"] == "ml" and h["to"] == "paper" for h in state.history)
