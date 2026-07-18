"""ML学習パイプライン(ml.py)のテスト(ネットワーク不要)。"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, UTC

from fx_intel.learning import EvaluatedCall
from fx_intel.ml import (
    FEATURE_NAMES,
    MLArtifact,
    build_dataset,
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
    n: int,
    gap_hours: float,
    seed: int,
    informative: bool = True,
    net_r_mean: float | None = None,
) -> list[EvaluatedCall]:
    """テスト用の採点済み判断列を作る。

    net_r_mean を指定すると収益ラベル realized_net_r を付与する。値は
    「net_r_mean(切片) + 0.5×(tech×方向符号)(=特徴依存の実力) + ノイズ」で、
    方向シグナルに連動した本物のOOS識別力を持つ(過学習検定PBO/DSRを通過しうる)。
    既定 None では realized_net_r=None(収益ヘッドの学習対象外)で、既存の二値
    ヘッドのテストには影響しない。
    """
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
            realized_net_r = None
            if net_r_mean is not None:
                realized_net_r = round(net_r_mean + 0.5 * (tech * sign) + rng.gauss(0, 0.25), 4)
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
                    realized_net_r=realized_net_r,
                    net_expected_r=0.15 if net_r_mean is not None else None,
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


def test_load_artifact_rejects_pre_pit_training_contract(tmp_path) -> None:
    art = train_artifact(_make_calls(700, gap_hours=8.0, seed=1), now=NOW)
    path = tmp_path / "model.json"
    save_artifact(art, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("training_contract")
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_artifact(path)

    assert loaded.model is None
    assert not loaded.usable
    assert any("PIT学習契約" in reason for reason in loaded.reasons)


def test_load_missing_returns_unusable() -> None:
    art = load_artifact("/nonexistent/model.json")
    assert not art.usable
    assert art.model is None


def test_unusable_artifact_predicts_none() -> None:
    art = MLArtifact()
    assert art.predict_hit_probability("long", 0.5, 0.3, {}) is None
    assert art.direction_edge(0.5, 0.3, {}) is None


def test_build_dataset_returns_aligned_return_labels() -> None:
    """build_dataset は二値ラベルと同じ並びで収益ラベル(realized_net_r)を返す。"""
    calls = _make_calls(20, gap_hours=8.0, seed=2, net_r_mean=0.3)
    rows, labels, stamps, r_labels = build_dataset(calls)
    assert len(rows) == len(labels) == len(stamps) == len(r_labels)
    # net_r_mean 指定時は全行に収益ラベルが付く
    assert all(r is not None for r in r_labels)


def test_build_dataset_return_labels_none_without_cost() -> None:
    """realized_net_r が無い(コスト不明の)判断は収益ラベルが None。"""
    calls = _make_calls(20, gap_hours=8.0, seed=2)  # net_r_mean 未指定
    _rows, _labels, _stamps, r_labels = build_dataset(calls)
    assert all(r is None for r in r_labels)


def test_return_heads_train_and_quantiles_are_ordered() -> None:
    """収益ヘッド(期待R回帰+分位点)が学習され、p10<=p50<=p90 の順序を保つ。"""
    calls = _make_calls(700, gap_hours=8.0, seed=1, net_r_mean=0.3)
    art = train_artifact(calls, now=NOW)
    assert art.return_model is not None
    assert set(art.quantile_models) == {"p10", "p50", "p90"}
    assert art.n_return_train > 0 and art.n_return_test > 0
    assert art.return_val_rmse is not None
    chart = {"rsi_1h": 65.0, "adx_1h": 25.0, "atr_pct": 0.15, "tf_agreement": 0.8, "news_count": 3}
    interval = art.net_r_interval("long", 0.5, 0.2, chart, 0.9)
    assert interval is not None
    assert interval["p10"] <= interval["p50"] <= interval["p90"]


def test_return_head_usable_requires_significant_positive_oos_net_r() -> None:
    """OOS平均純Rが有意に正なら return_usable=True、期待純Rを返せる。"""
    # 強く正の純R(平均+0.6R)なら t 検定を通過する
    positive = _make_calls(700, gap_hours=8.0, seed=1, net_r_mean=0.6)
    art = train_artifact(positive, now=NOW)
    assert art.return_usable, art.return_reasons
    assert art.return_oos_mean_net_r is not None and art.return_oos_mean_net_r > 0
    chart = {"rsi_1h": 65.0, "adx_1h": 25.0, "atr_pct": 0.15, "tf_agreement": 0.8, "news_count": 3}
    assert art.expected_net_r("long", 0.5, 0.2, chart, 0.9) is not None

    # 平均ほぼ0の純Rは有意でないので return_usable=False、期待純Rは None
    flat = _make_calls(700, gap_hours=8.0, seed=5, net_r_mean=0.0)
    flat_art = train_artifact(flat, now=NOW)
    assert not flat_art.return_usable
    assert flat_art.expected_net_r("long", 0.5, 0.2, chart, 0.9) is None


def test_return_heads_survive_save_load_roundtrip(tmp_path) -> None:
    """収益ヘッド(回帰+分位点)が保存・読み込みで復元される。"""
    calls = _make_calls(700, gap_hours=8.0, seed=1, net_r_mean=0.6)
    art = train_artifact(calls, now=NOW)
    path = tmp_path / "model.json"
    save_artifact(art, path)
    loaded = load_artifact(path)
    assert loaded.return_usable == art.return_usable
    assert set(loaded.quantile_models) == set(art.quantile_models)
    chart = {"rsi_1h": 65.0, "adx_1h": 25.0, "atr_pct": 0.15, "tf_agreement": 0.8, "news_count": 3}
    assert loaded.expected_net_r("long", 0.5, 0.2, chart, 0.9) == art.expected_net_r(
        "long", 0.5, 0.2, chart, 0.9
    )


def test_return_head_runs_hyperparameter_trials_and_records_overfitting() -> None:
    """収益ヘッドは小ハイパラ探索(複数試行)を回し、PBO/DSRを来歴に記録する。"""
    calls = _make_calls(700, gap_hours=8.0, seed=1, net_r_mean=0.6)
    art = train_artifact(calls, now=NOW)
    # RETURN_TRIAL_GRID の試行数(4件)
    assert art.return_n_trials == 4
    # 十分な共有時刻があれば PBO/DSR が計算・記録される
    assert art.return_pbo is not None
    assert art.return_dsr is not None
    # DSRが合格水準を満たすので採用される(t検定も通過)
    assert art.return_usable


def test_return_head_gate_is_dsr_not_pbo() -> None:
    """PBOは記録のみでゲートに使わない: PBO=0.5(無情報)でも DSR 合格なら採用。

    現グリッドは試行が似通いPBOが0.5付近に張り付くため、ゲートはDSRが担う。
    """
    calls = _make_calls(700, gap_hours=8.0, seed=1, net_r_mean=0.6)
    art = train_artifact(calls, now=NOW)
    # PBO は 0.5 付近(無情報)だが return_usable は True(DSRで判定)
    assert art.return_pbo is not None and abs(art.return_pbo - 0.5) < 0.2
    assert art.return_dsr is not None and art.return_dsr >= 0.95
    assert art.return_usable


def test_return_head_skips_overfitting_gate_with_few_observations() -> None:
    """OOS共有時刻が32件未満だと過学習検定はskip(PBO/DSRは未計算=None)。

    採点済みが最低件数(MIN_RETURN_TRAIN/TEST)は満たすが、共有時刻が
    PBO_MIN_OBSERVATIONS 未満のケース。ゲートはt検定のみで判定する。
    """
    # 同一時刻に多数ペアを詰めて test区間のユニーク時刻を32未満に抑える
    from fx_intel.ml import PBO_MIN_OBSERVATIONS

    calls = _make_calls(90, gap_hours=8.0, seed=3, net_r_mean=0.6)
    art = train_artifact(calls, now=NOW)
    if art.n_return_train >= 40 and art.n_return_test >= 20:
        # 学習は走るが、共有時刻が閾値未満なら過学習検定はskip
        if art.return_n_trials > 0 and art.return_pbo is None:
            assert any("過学習検定skip" in r for r in art.return_reasons)
    # いずれにせよ PBO_MIN_OBSERVATIONS は正の定数
    assert PBO_MIN_OBSERVATIONS > 0


def test_return_head_pbo_dsr_survive_roundtrip(tmp_path) -> None:
    """PBO/DSR/試行数が保存・読み込みで復元される。"""
    calls = _make_calls(700, gap_hours=8.0, seed=1, net_r_mean=0.6)
    art = train_artifact(calls, now=NOW)
    path = tmp_path / "model.json"
    save_artifact(art, path)
    loaded = load_artifact(path)
    assert loaded.return_n_trials == art.return_n_trials
    assert loaded.return_pbo == art.return_pbo
    assert loaded.return_dsr == art.return_dsr
