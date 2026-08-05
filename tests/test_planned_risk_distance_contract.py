"""`planned_risk_distance` 契約の監査テスト。ネットワーク不要。

⚠️ このテスト群は「現状の契約がこうである」ことを固定する監査用であり、
ガードを追加するものではない。特に「下限ガードが存在しない」ことを明示的に
固定しているので、将来ガードを入れる際は**このテストが落ちる**。
落ちたら期待値を更新すること(気づかず素通りしないため)。
"""

from __future__ import annotations

import json
from pathlib import Path

from fx_intel.briefing import DEFAULT_ATR_MULTIPLE, freeze_target_plan
from fx_intel.evaluation_labels import canonical_net_label_contract_flags

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "canonical_outcome_full_cost.json"


def _record() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_planned_risk_distance_is_atr_times_multiple() -> None:
    """本番の stop 幅は ATR × 倍率のみで決まる。"""
    plan = freeze_target_plan(
        "USDJPY",
        "long",
        50,
        close=150.0,
        atr=0.2,
        atr_multiple=DEFAULT_ATR_MULTIPLE,
        target_r_adjuster=None,
    )
    assert plan.planned_risk_distance == 0.2 * DEFAULT_ATR_MULTIPLE
    # stop は close から stop 幅ぶん離れる(long なら下)
    assert plan.stop == 150.0 - 0.2 * DEFAULT_ATR_MULTIPLE


def test_production_atr_multiple_is_a_single_constant() -> None:
    """本番倍率は定数であり、通貨・時間足で分岐しない。

    ⚠️ 時間足別倍率(COUNTERFACTUAL_TIMEFRAME_ATR_MULTIPLE)は shadow 専用で
    本番判断に接続していないが、その定義は未追跡の作業ツリーにしか無いため
    ここでは import しない(clean clone で collection error になるのを避ける)。
    本番側が定数であることだけを固定する。
    """
    assert DEFAULT_ATR_MULTIPLE == 2.5

    # 同じ ATR なら symbol/timeframe が違っても stop 幅は同じ = 分岐が無い証拠
    first = freeze_target_plan(
        "USDJPY",
        "long",
        50,
        close=150.0,
        atr=0.2,
        atr_multiple=DEFAULT_ATR_MULTIPLE,
        target_r_adjuster=None,
    )
    second = freeze_target_plan(
        "EURUSD",
        "short",
        90,
        close=1.15,
        atr=0.2,
        atr_multiple=DEFAULT_ATR_MULTIPLE,
        target_r_adjuster=None,
    )
    assert first.planned_risk_distance == second.planned_risk_distance


def test_tiny_atr_produces_tiny_stop_without_any_floor() -> None:
    """⚠️現状の契約: stop 幅に下限が無い。

    極端に小さい ATR をそのまま通すと、spread より狭い stop が生成される。
    これは実機で3/48件(6.2%)発生していた事象の再現。

    将来 stop 幅の下限ガードを入れたら**このテストは落ちる**。
    その時は「ガードが入った」ことを確認して期待値を更新すること。
    """
    plan = freeze_target_plan(
        "EURUSD",
        "short",
        50,
        close=1.15071,
        atr=0.00001457,  # 実機の病的レコードから逆算した ATR
        atr_multiple=DEFAULT_ATR_MULTIPLE,
        target_r_adjuster=None,
    )
    assert plan.planned_risk_distance is not None
    # 典型的な EURUSD spread(0.00005)より stop が狭い
    assert plan.planned_risk_distance < 0.00005
    # それでも契約上は有効な plan として返る(下限で弾かれない)
    assert plan.stop is not None


def test_only_positivity_is_enforced_on_planned_risk_distance() -> None:
    """契約が課すのは `> 0` だけで、下限は課されない。"""
    base = _record()

    # 0 は拒否される
    zero = {**base, "planned_risk_distance": 0.0}
    assert "invalid_planned_risk_distance" in canonical_net_label_contract_flags(zero)

    # 極小でも「invalid」にはならない(下限が無いことの固定)
    tiny = dict(base)
    tiny["planned_risk_distance"] = 1e-9
    flags = canonical_net_label_contract_flags(tiny)
    assert "invalid_planned_risk_distance" not in flags


def test_entry_spread_r_is_recorded_but_never_rejects_a_trade() -> None:
    """⚠️現状の契約: entry_spread_r は算術検算のみで、拒否には使われない。

    spread が stop 幅を上回る(ratio > 1.0)取引でも、契約は
    `entry_spread_r_mismatch` を出さない限り通過する。
    実機で3/48件がこの状態だった。
    """
    base = _record()
    entry_bid = float(base["entry_bid"])  # type: ignore[arg-type]
    entry_ask = float(base["entry_ask"])  # type: ignore[arg-type]
    spread = entry_ask - entry_bid

    # spread より狭い stop 幅に差し替え、entry_spread_r を整合させる
    narrow = spread / 1.37  # 実機の最悪ケースと同じ比率
    pathological = dict(base)
    pathological["planned_risk_distance"] = narrow
    pathological["entry_spread_r"] = spread / narrow

    flags = canonical_net_label_contract_flags(pathological)
    # 算術は整合しているので mismatch は出ない
    assert "entry_spread_r_mismatch" not in flags
    # そして「spread が stop を超えている」ことを咎める flag は存在しない
    assert not any("spread" in flag and "exceed" in flag for flag in flags)
    assert pathological["entry_spread_r"] > 1.0
