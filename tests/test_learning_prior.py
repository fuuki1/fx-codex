"""履歴prior(セル間分散の上限)と、それを使う収縮の契約テスト。

受け入れゲート(docs/design/P1_HISTORICAL_PRIOR.md §6)に対応する:

- G1/G2 後方互換と数学的等価: prior=None と tau→∞ が同じ結果を出す
- G3 偽陽性除去: コイン投げ入力での偽減衰セルが実質ゼロになる
- G4 推定器の陽性対照: 既知のtauを回収できる
- G5 真の劣位の検出保持: 分散が本当にある世界では調整が出る
- G6 決定論: 同一コーパスから同一artifactが出る
"""

from __future__ import annotations

import json
import random
import statistics
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

from fx_intel import learning, learning_prior
from tools import build_learning_prior

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_ARTIFACT = REPO_ROOT / "config" / "learning_prior_v1.json"
START = datetime(2026, 1, 5, tzinfo=UTC)


def _features(rng: random.Random) -> dict[str, float]:
    return {
        "rsi_1h": max(1.0, min(99.0, rng.gauss(50.0, 13.0))),
        "adx_1h": max(3.0, rng.gauss(23.0, 9.0)),
        "ma_gap_atr": abs(rng.gauss(0.0, 1.1)),
        "atr_pct": max(0.01, rng.lognormvariate(-2.2, 0.55)),
    }


def _calls(count: int, rng: random.Random, hit_rate: float = 0.5) -> list[learning.EvaluatedCall]:
    """1時間間隔の採点済み判断。結果は特徴量と無関係(既定でコイン投げ)。"""
    return [
        learning.EvaluatedCall(
            symbol="USDJPY",
            direction=rng.choice(["long", "short"]),
            conviction=rng.randint(20, 80),
            tech_score=rng.uniform(-1.0, 1.0),
            news_score=rng.uniform(-1.0, 1.0),
            outcome="hit" if rng.random() < hit_rate else "miss",
            ts=(START + timedelta(hours=index)).isoformat(),
            features=_features(rng),
        )
        for index in range(count)
    ]


def _prior(tau: float, features: tuple[str, ...] = ("rsi_1h", "adx_1h", "ma_gap_atr", "atr_pct")):
    return learning_prior.LearningPrior(
        contract=learning_prior.PRIOR_CONTRACT,
        generated_at="2026-08-23T00:00:00+00:00",
        corpus_id="test",
        slices={
            learning_prior.FUSION_KEY: {
                name: learning_prior.FeaturePrior(
                    feature=name, tau=tau, tau_hat=tau, cells=6, q_statistic=5.0, n_eff_median=100.0
                )
                for name in features
            }
        },
    )


def _fired_cells(profile: learning.LearnedProfile) -> int:
    return sum(
        len(directions)
        for buckets in profile.condition_factors.values()
        for directions in buckets.values()
    )


# --- G2: 現行挙動は tau -> ∞ の特殊ケース ------------------------------------


def test_infinite_tau_reproduces_the_unshrunk_profile() -> None:
    calls = _calls(600, random.Random(11))
    baseline = learning.derive_profile(calls)
    shrunk = learning.derive_profile(calls, prior=_prior(1e9))
    assert shrunk.condition_factors == baseline.condition_factors
    assert baseline.prior_taus == {}
    assert shrunk.prior_taus  # 来歴は記録される


def test_prior_none_leaves_every_field_untouched() -> None:
    calls = _calls(400, random.Random(12))
    profile = learning.derive_profile(calls, prior=None)
    assert profile.prior_taus == {}
    assert profile.prior_corpus_id == ""
    assert profile.prior_slice == ""


def test_unknown_slice_falls_back_to_current_behaviour() -> None:
    calls = _calls(600, random.Random(13))
    baseline = learning.derive_profile(calls)
    other = learning.derive_profile(calls, prior=_prior(0.0), prior_slice="tf:15m")
    assert other.condition_factors == baseline.condition_factors
    assert other.prior_taus == {}


def test_excluded_parity_is_not_used_for_shrinkage() -> None:
    calls = _calls(600, random.Random(14))
    baseline = learning.derive_profile(calls)
    prior = learning_prior.LearningPrior(
        contract=learning_prior.PRIOR_CONTRACT,
        generated_at="",
        corpus_id="test",
        slices={
            learning_prior.FUSION_KEY: {
                name: learning_prior.FeaturePrior(
                    feature=name,
                    tau=0.0,
                    tau_hat=0.0,
                    cells=6,
                    q_statistic=1.0,
                    n_eff_median=10.0,
                    parity=learning_prior.PARITY_EXCLUDED,
                )
                for name in ("rsi_1h", "adx_1h", "ma_gap_atr", "atr_pct")
            }
        },
    )
    assert learning.derive_profile(calls, prior=prior).condition_factors == (
        baseline.condition_factors
    )


# --- G3: 無情報入力での偽減衰がほぼ消える -------------------------------------


@pytest.mark.parametrize("count", [200, 500, 1000])
def test_prior_removes_false_decay_under_a_coin_flip_null(count: int) -> None:
    """真の優位ゼロの入力で、現行は多数のセルが減衰し、priorありでは実質ゼロになる。"""
    committed = learning_prior.load_prior(COMMITTED_ARTIFACT)
    assert committed is not None
    taus = [
        entry.tau for entry in committed.slices[learning_prior.FUSION_KEY].values() if entry.tau > 0
    ]
    prior = _prior(max(taus))  # 実測上限のうち最も緩いものを使う(保守的な検証)

    without: list[int] = []
    with_prior: list[int] = []
    for seed in range(20):
        calls = _calls(count, random.Random(500 + seed))
        without.append(_fired_cells(learning.derive_profile(calls)))
        with_prior.append(_fired_cells(learning.derive_profile(calls, prior=prior)))
    assert statistics.mean(without) > 1.0  # 現行は誤検出する
    assert statistics.mean(with_prior) < 0.1  # priorありでは実質ゼロ
    assert max(with_prior) == 0


# --- G5: 分散が本当にある世界では調整が残る -----------------------------------


def test_wide_prior_still_allows_adjustment_when_a_cell_is_truly_bad() -> None:
    rng = random.Random(77)
    calls: list[learning.EvaluatedCall] = []
    for index in range(4000):
        features = _features(rng)
        direction = rng.choice(["long", "short"])
        # RSI売られすぎ圏のロングだけ真に不利(30%)。他は五分。
        rate = 0.30 if (features["rsi_1h"] < 35.0 and direction == "long") else 0.5
        calls.append(
            learning.EvaluatedCall(
                symbol="USDJPY",
                direction=direction,
                conviction=50,
                tech_score=0.0,
                news_score=0.0,
                outcome="hit" if rng.random() < rate else "miss",
                ts=(START + timedelta(hours=index)).isoformat(),
                features=features,
            )
        )
    profile = learning.derive_profile(calls, prior=_prior(0.05))
    assert "long" in profile.condition_factors.get("rsi_1h", {}).get("売られすぎ圏(35未満)", {})


# --- G4: 推定器の陽性対照 -----------------------------------------------------


def _recover_tau(
    tau_true: float, seed: int, points: int = 18000, overlap: int = 4
) -> tuple[float, float]:
    rng = random.Random(seed)
    offsets = {cell: rng.gauss(0.0, tau_true) for cell in range(6)}
    counts = [[0, 0] for _ in range(6)]
    for _ in range(points):
        cell = rng.choices(range(6), weights=[0.25, 0.55, 0.20, 0.25, 0.55, 0.20])[0]
        rate = min(0.95, max(0.05, 0.5 + offsets[cell]))
        counts[cell][0] += 1
        counts[cell][1] += int(rng.random() < rate)
    deviations = [counts[c][1] / counts[c][0] - 0.5 for c in range(6)]
    variances = [0.25 / (counts[c][0] / overlap) for c in range(6)]
    tau_hat, tau_upper, _q = learning_prior.dersimonian_laird(deviations, variances)
    return tau_hat, tau_upper


def test_estimator_recovers_injected_dispersion() -> None:
    recovered = [_recover_tau(0.03, seed) for seed in range(5)]
    assert statistics.mean(hat for hat, _upper in recovered) > 0.01
    assert statistics.mean(upper for _hat, upper in recovered) > 0.02


def test_estimator_stays_near_zero_without_dispersion() -> None:
    recovered = [_recover_tau(0.0, seed) for seed in range(5)]
    # 点推定は0へ潰れる。上限は片側95%なので0より大きくてよいが、小さく留まる。
    assert max(hat for hat, _upper in recovered) == 0.0
    assert max(upper for _hat, upper in recovered) < 0.02


def test_chi_square_quantile_matches_published_values() -> None:
    assert learning_prior.chi_square_quantile(0.05, 5) == pytest.approx(1.145476, abs=1e-4)
    assert learning_prior.chi_square_quantile(0.05, 23) == pytest.approx(13.09051, abs=1e-3)
    assert learning_prior.chi_square_quantile(0.95, 1) == pytest.approx(3.841459, abs=1e-4)


# --- 収縮そのものの性質 -------------------------------------------------------


def test_shrink_rate_boundaries() -> None:
    assert learning_prior.shrink_rate(0.30, 12, 0.5, 0.0) == 0.5
    assert learning_prior.shrink_rate(0.30, 12, 0.5, 1e9) == pytest.approx(0.30, abs=1e-9)
    assert learning_prior.shrink_rate(0.30, 0, 0.5, 0.02) == 0.30


def test_shrink_rate_is_monotone_in_samples_and_tau() -> None:
    small = learning_prior.shrink_rate(0.30, 12, 0.5, 0.02)
    large = learning_prior.shrink_rate(0.30, 2000, 0.5, 0.02)
    assert 0.30 <= large < small <= 0.5
    loose = learning_prior.shrink_rate(0.30, 100, 0.5, 0.10)
    tight = learning_prior.shrink_rate(0.30, 100, 0.5, 0.01)
    assert loose < tight


# --- artifact の契約と fail-open ---------------------------------------------


def test_committed_artifact_matches_the_contract() -> None:
    payload = json.loads(COMMITTED_ARTIFACT.read_text(encoding="utf-8"))
    assert payload["contract"] == learning_prior.PRIOR_CONTRACT
    assert payload["schema"] == learning_prior.SCHEMA_VERSION
    assert payload["promotion_admissible"] is False
    assert payload["payload_sha256"] == build_learning_prior._payload_digest(payload)
    for dataset in payload["corpus"]["datasets"]:
        raw = (REPO_ROOT / dataset["path"]).read_bytes()
        import hashlib

        assert hashlib.sha256(raw).hexdigest() == dataset["sha256"]
    prior = learning_prior.load_prior(COMMITTED_ARTIFACT)
    assert prior is not None
    assert learning_prior.FUSION_KEY in prior.slices
    for features in prior.slices.values():
        for entry in features.values():
            assert 0.0 <= entry.tau < 0.5
            assert entry.cells >= 2


def test_load_prior_fails_open(tmp_path: Path) -> None:
    assert learning_prior.load_prior(tmp_path / "missing.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert learning_prior.load_prior(broken) is None
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"contract": "other", "schema": 1}), encoding="utf-8")
    assert learning_prior.load_prior(wrong) is None
    empty = tmp_path / "empty.json"
    empty.write_text(
        json.dumps(
            {
                "contract": learning_prior.PRIOR_CONTRACT,
                "schema": learning_prior.SCHEMA_VERSION,
                "slices": {},
            }
        ),
        encoding="utf-8",
    )
    assert learning_prior.load_prior(empty) is None


# --- G6: 生成の決定論 ---------------------------------------------------------


def _write_corpus(directory: Path, name: str, seed: int, bars: int = 900) -> str:
    rng = random.Random(seed)
    path = directory / name
    price = 150.0
    lines = ["timestamp,open,high,low,close"]
    stamp = datetime(2024, 1, 1, 22, 0, tzinfo=UTC)
    for _ in range(bars):
        move = rng.gauss(0.0, 0.08)
        high = price + abs(move) + 0.02
        low = price - abs(move) - 0.02
        close = price + move
        lines.append(
            f"{stamp.isoformat()},{price:.3f},{high:.3f},{low:.3f},{close:.3f}",
        )
        price = close
        stamp += timedelta(hours=1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def test_generator_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_learning_prior, "REPO_ROOT", tmp_path)
    _write_corpus(tmp_path, "a.csv", 1)
    _write_corpus(tmp_path, "b.csv", 2)
    sources = (("USDJPY", "a.csv"), ("EURUSD", "b.csv"))
    first = build_learning_prior.build_payload(sources, "unit-test")
    second = build_learning_prior.build_payload(sources, "unit-test")
    assert first["payload_sha256"] == second["payload_sha256"]
    assert build_learning_prior.render(first) == build_learning_prior.render(second)
    prior = learning_prior.prior_from_mapping(first)
    assert prior is not None
    assert learning_prior.FUSION_KEY in prior.slices


def test_generator_demeans_before_measuring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一方向へ強くトレンドする系列でも、de-mean後の上限は暴走しない。"""
    monkeypatch.setattr(build_learning_prior, "REPO_ROOT", tmp_path)
    path = tmp_path / "trend.csv"
    price = 100.0
    lines = ["timestamp,open,high,low,close"]
    stamp = datetime(2024, 1, 1, 22, 0, tzinfo=UTC)
    rng = random.Random(5)
    for _ in range(1200):
        close = price + 0.02 + rng.gauss(0.0, 0.05)  # 明確な上昇トレンド
        lines.append(
            f"{stamp.isoformat()},{price:.3f},{max(price, close) + 0.02:.3f},"
            f"{min(price, close) - 0.02:.3f},{close:.3f}"
        )
        price = close
        stamp += timedelta(hours=1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = build_learning_prior.build_payload((("USDJPY", "trend.csv"),), "trend-test")
    slices = payload["slices"]
    assert isinstance(slices, dict)
    for features in slices.values():
        for entry in features.values():
            # トレンドが焼き込まれていれば上限は数十%になる。de-mean後はそうならない。
            assert entry["tau_upper95"] < 0.15
