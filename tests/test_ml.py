"""ML学習パイプライン(ml.py)のテスト(ネットワーク不要)。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, UTC

import pytest

from fx_intel.gbm import GradientBoostingRegressor, rmse
from fx_intel.learning import EvaluatedCall
from fx_intel.ml import (
    FEATURE_NAMES,
    MLArtifact,
    build_return_dataset,
    compute_medians,
    direction_features,
    load_artifact,
    save_artifact,
    thin_calls,
    train_artifact,
    vectorize,
)

NOW = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)
START = datetime(2026, 1, 6, 0, 0, tzinfo=UTC)


def _make_calls(
    n: int, gap_hours: float, seed: int, informative: bool = True
) -> list[EvaluatedCall]:
    rng = random.Random(seed)
    calls: list[EvaluatedCall] = []
    for i in range(n):
        ts = START + timedelta(hours=gap_hours * i)
        for symbol in ("USDJPY", "EURUSD"):
            direction = rng.choice(["long", "short"])
            sign = 1.0 if direction == "long" else -1.0
            tech = rng.uniform(-0.8, 0.8)
            news = rng.uniform(-0.5, 0.5)
            rsi = rng.uniform(25, 75)
            if informative:
                edge = 1.0 * tech * sign - 0.4 * ((rsi - 50) / 50) * sign
                p_hit = 1.0 / (1.0 + pow(2.71828, -2.0 * edge))
            else:
                p_hit = 0.5
            outcome = "hit" if rng.random() < p_hit else "miss"
            calls.append(
                EvaluatedCall(
                    symbol=symbol,
                    direction=direction,
                    conviction=50,
                    tech_score=tech,
                    news_score=news,
                    outcome=outcome,
                    ts=ts.isoformat(),
                    features={
                        "rsi_1h": rsi,
                        "adx_1h": rng.uniform(10, 40),
                        "atr_pct": 0.15,
                        "tf_agreement": rng.random(),
                        "news_count": 3,
                        "ma_gap_atr": rng.uniform(-2, 2),
                    },
                    move_atr=rng.gauss(0.2 if outcome == "hit" else -0.2, 0.4),
                    data_quality=0.9,
                )
            )
    return calls


def _make_return_outcomes(n: int, seed: int = 3) -> list[dict[str, object]]:
    rng = random.Random(seed)
    outcomes: list[dict[str, object]] = []
    for index in range(n):
        direction = "long" if index % 2 == 0 else "short"
        sign = 1.0 if direction == "long" else -1.0
        tech = rng.uniform(-1.0, 1.0)
        net_r = 0.8 * tech * sign + rng.gauss(0.0, 0.08)
        outcomes.append(
            {
                "ts": (START + timedelta(hours=8 * index)).isoformat(),
                "decision_id": f"return-{index}",
                "symbol": "USDJPY" if index % 3 else "EURUSD",
                "direction": direction,
                "tech_score": tech,
                "news_score": 0.0,
                "data_quality": 0.95,
                "features": {"adx_1h": 25.0, "atr_pct": 0.15},
                "realized_r": net_r + 0.1,
                "realized_net_r": net_r,
                "tradable": True,
                "net_label_eligible": True,
                "label_version": "net-r-v1",
                "cost_model_id": "test-quotes-v1",
            }
        )
    return outcomes


def test_direction_features_sign_flip() -> None:
    chart = {"rsi_1h": 70.0, "ma_gap_atr": 1.0, "adx_1h": 25.0}
    long_f = direction_features("long", 0.5, 0.3, chart)
    short_f = direction_features("short", 0.5, 0.3, chart)
    for key in ("dir_tech", "dir_rsi"):
        long_v, short_v = long_f[key], short_f[key]
        assert long_v is not None and short_v is not None
        assert long_v == -short_v  # ロングとショートで符号が反転する


def test_direction_features_htf_rating() -> None:
    """上位足レーティングは4h/1dの平均×方向符号(順行+/逆行-)。"""
    chart = {"rating_4h": 1.0, "rating_1d": 0.5}
    assert direction_features("long", 0.0, 0.0, chart)["dir_htf"] == 0.75
    assert direction_features("short", 0.0, 0.0, chart)["dir_htf"] == -0.75
    # 片方欠損なら取得できた方のみ、両方欠損ならNone(中央値補完に回す)
    assert direction_features("long", 0.0, 0.0, {"rating_4h": -1.0})["dir_htf"] == -1.0
    assert direction_features("long", 0.0, 0.0, {})["dir_htf"] is None


def test_direction_features_one_hot_session_and_regime() -> None:
    row = direction_features(
        "long",
        0.0,
        0.0,
        {},
        dimensions={"session_bucket": "london_new_york_overlap", "regime": "risk_off"},
    )
    assert row["session_london_new_york_overlap"] == 1.0
    assert row["session_london"] == 0.0
    assert row["regime_risk_off"] == 1.0
    assert row["regime_risk_on"] == 0.0


def test_direction_features_include_macro_and_liquidity_context() -> None:
    chart = {
        "macro__macro_pair_score": -0.4,
        "macro__vix_level": 26.0,
        "macro__cot_pair_diff": 0.2,
        "macro__vix_level__available": 1.0,
        "macro__cot_pair_diff__available": 1.0,
        "liquidity__spread_pips": 1.3,
        "liquidity__spread_percentile": 0.95,
        "liquidity__spread_atr": 0.08,
        "liquidity__status_thin": 1.0,
        "liquidity__spread_pips__available": 1.0,
        "liquidity__spread_percentile__available": 1.0,
    }

    long = direction_features("long", 0.0, 0.0, chart)
    short = direction_features("short", 0.0, 0.0, chart)

    assert long["dir_macro"] == -0.4
    assert short["dir_macro"] == 0.4
    assert long["dir_cot_pair_diff"] == 0.2
    assert short["dir_cot_pair_diff"] == -0.2
    assert long["macro_vix_level"] == 26.0
    assert long["liquidity_spread_percentile"] == 0.95
    assert long["liquidity_status_thin"] == 1.0
    assert long["liquidity_baseline_available"] == 1.0


def test_thin_calls_enforces_gap() -> None:
    # 1時間おき100件 → 4時間ゲートで最大約25件に間引かれる
    calls = _make_calls(100, gap_hours=1.0, seed=1)
    usdjpy = [c for c in calls if c.symbol == "USDJPY"]
    thinned = [c for c in thin_calls(usdjpy) if c.symbol == "USDJPY"]
    assert len(thinned) < len(usdjpy)
    assert len(thinned) <= 26


def test_vectorize_imputes_missing_with_median() -> None:
    rows: list[dict[str, float | None]] = [{"adx_1h": 20.0}, {"adx_1h": 30.0}, {"adx_1h": None}]
    medians = compute_medians(rows)
    vec = vectorize({"adx_1h": None}, medians)
    idx = FEATURE_NAMES.index("adx_1h")
    assert vec[idx] == medians["adx_1h"]


def test_gradient_boosting_regressor_learns_continuous_target() -> None:
    features = [[index / 100.0] for index in range(100)]
    labels = [2.0 * row[0] - 0.5 for row in features]
    model = GradientBoostingRegressor(min_samples_leaf=5, seed=2)
    model.fit(features[:80], labels[:80], features[80:], labels[80:])
    predictions = model.predict_many(features[80:])
    baseline = [sum(labels[:80]) / 80] * 20
    assert rmse(labels[80:], predictions) < rmse(labels[80:], baseline)


def test_return_dataset_rejects_mixed_accounting_versions() -> None:
    outcomes = _make_return_outcomes(4)
    outcomes[-1]["cost_model_id"] = "different-model"
    with pytest.raises(ValueError, match="混在"):
        build_return_dataset(outcomes)


def test_train_artifact_builds_shadow_return_head_from_canonical_labels(tmp_path) -> None:
    outcomes = _make_return_outcomes(240)
    artifact = train_artifact([], return_outcomes=outcomes, now=NOW, min_return_rows=80)
    assert artifact.return_model is not None
    assert artifact.return_usable, artifact.return_reasons
    assert artifact.return_label_version == "net-r-v1"
    assert artifact.return_cost_model_id == "test-quotes-v1"
    expected = artifact.expected_net_r("long", 0.8, 0.0, {"adx_1h": 25.0}, 0.95)
    interval = artifact.net_r_interval("long", 0.8, 0.0, {"adx_1h": 25.0}, 0.95)
    assert expected is not None and expected > 0
    assert interval is not None and interval[0] <= interval[1] <= interval[2]
    path = tmp_path / "return-model.json"
    save_artifact(artifact, path)
    loaded = load_artifact(path)
    assert loaded.return_usable is True
    assert loaded.expected_net_r("long", 0.8, 0.0, {"adx_1h": 25.0}, 0.95) == expected


def test_train_artifact_learns_usable_model() -> None:
    calls = _make_calls(700, gap_hours=8.0, seed=1, informative=True)
    art = train_artifact(calls, now=NOW)
    assert art.usable, art.reasons
    assert art.val_brier is not None and art.baseline_brier is not None
    assert art.val_brier < art.baseline_brier


def test_train_artifact_rejects_noise() -> None:
    """情報のないデータではスキルゲートで usable=False になる。"""
    calls = _make_calls(700, gap_hours=8.0, seed=9, informative=False)
    art = train_artifact(calls, now=NOW)
    assert not art.usable


def test_train_artifact_insufficient_data() -> None:
    calls = _make_calls(20, gap_hours=8.0, seed=1)
    art = train_artifact(calls, now=NOW)
    assert not art.usable
    assert any("不足" in r for r in art.reasons)


def test_direction_edge_reflects_signal() -> None:
    calls = _make_calls(700, gap_hours=8.0, seed=1, informative=True)
    art = train_artifact(calls, now=NOW)
    assert art.usable
    chart = {
        "rsi_1h": 50.0,
        "adx_1h": 25.0,
        "atr_pct": 0.15,
        "tf_agreement": 0.8,
        "news_count": 3,
        "ma_gap_atr": 0.0,
    }
    bull = art.direction_edge(0.7, 0.3, chart, 0.9)
    bear = art.direction_edge(-0.7, -0.3, chart, 0.9)
    assert bull is not None and bear is not None
    assert bull[0] > bull[1]  # 強気材料ではロングの的中確率が高い
    assert bear[1] > bear[0]  # 弱気材料ではショートの的中確率が高い


def test_artifact_serialization_roundtrip(tmp_path) -> None:
    calls = _make_calls(700, gap_hours=8.0, seed=1, informative=True)
    art = train_artifact(calls, now=NOW)
    path = tmp_path / "model.json"
    save_artifact(art, path)
    loaded = load_artifact(path)
    assert loaded.usable == art.usable
    chart = {
        "rsi_1h": 40.0,
        "adx_1h": 25.0,
        "atr_pct": 0.15,
        "tf_agreement": 0.8,
        "news_count": 3,
        "ma_gap_atr": 1.0,
    }
    assert loaded.direction_edge(0.5, 0.2, chart, 0.9) == art.direction_edge(0.5, 0.2, chart, 0.9)


def test_load_missing_returns_unusable() -> None:
    art = load_artifact("/nonexistent/model.json")
    assert not art.usable
    assert art.model is None


def test_unusable_artifact_predicts_none() -> None:
    art = MLArtifact()
    assert art.predict_hit_probability("long", 0.5, 0.3, {}) is None
    assert art.direction_edge(0.5, 0.3, {}) is None
