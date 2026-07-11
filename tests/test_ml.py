"""ML学習パイプライン(ml.py)のテスト(ネットワーク不要)。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, UTC

from fx_intel.learning import EvaluatedCall
from fx_intel.ml import (
    FEATURE_NAMES,
    MLArtifact,
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


def test_train_artifact_learns_usable_model() -> None:
    calls = _make_calls(700, gap_hours=8.0, seed=1, informative=True)
    art = train_artifact(calls, now=NOW)
    assert art.usable, art.reasons
    assert art.val_brier is not None and art.baseline_brier is not None
    assert art.val_brier < art.baseline_brier
    assert art.test_auc is not None and art.test_auc >= 0.55
    assert art.n_tune >= 30
    assert art.n_calibration >= 30
    assert art.n_test >= 30
    assert art.n_lockbox >= 30
    assert not art.lockbox_evaluated
    assert set(art.partition_windows) == {"train", "tune", "calibration", "test", "lockbox"}
    assert art.partition_windows["calibration"]["end"] < art.partition_windows["test"]["start"]


def test_train_artifact_rejects_noise() -> None:
    """情報のないデータではスキルゲートで usable=False になる。"""
    calls = _make_calls(700, gap_hours=8.0, seed=9, informative=False)
    art = train_artifact(calls, now=NOW)
    assert not art.usable


def test_constant_features_and_prevalence_shift_cannot_pass_skill_gate() -> None:
    calls: list[EvaluatedCall] = []
    for index in range(1_000):
        # Train/tune are 50%; calibration/test are 70%. Predictors are constant,
        # so Platt scaling can learn only the later prevalence/intercept.
        later_window = 650 <= index < 900
        hit = index % 10 < (7 if later_window else 5)
        calls.append(
            EvaluatedCall(
                symbol="USDJPY",
                direction="long",
                conviction=50,
                tech_score=0.0,
                news_score=0.0,
                outcome="hit" if hit else "miss",
                ts=(START + timedelta(hours=8 * index)).isoformat(),
                features={
                    "rsi_1h": 50.0,
                    "adx_1h": 20.0,
                    "atr_pct": 0.1,
                    "tf_agreement": 0.5,
                    "news_count": 1.0,
                    "ma_gap_atr": 0.0,
                },
                move_atr=0.1 if hit else -0.1,
                data_quality=1.0,
            )
        )

    artifact = train_artifact(calls, now=NOW)

    assert not artifact.usable
    assert artifact.test_auc == 0.5
    assert artifact.importance_by_name == {}
    assert any("特徴量識別 なし" in reason for reason in artifact.reasons)


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
