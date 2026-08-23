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
import math
import random
import statistics
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

from fx_intel import journal, learning, learning_prior
from tools import build_learning_prior, learning_prior_audit

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


# --- G7/G8: 運用ジャーナルでの検証（tools/learning_prior_audit.py） ------------


def _weekday_hours(start: datetime, count: int) -> list[datetime]:
    """市場オープン中(平日)の1時間刻み時刻列。週末はまたがずに飛ばす。"""
    stamps: list[datetime] = []
    cursor = start
    while len(stamps) < count:
        if cursor.weekday() < 5:
            stamps.append(cursor)
        cursor += timedelta(hours=1)
    return stamps


def _fusion_journal(path: Path, count: int = 700, bad_cell: bool = False, seed: int = 3) -> None:
    """PIT適格な融合ジャーナルを合成する。24市場時間先の終値で採点される。"""
    rng = random.Random(seed)
    stamps = _weekday_hours(datetime(2026, 1, 5, 0, 0, tzinfo=UTC), count)
    prices = [150.0]
    for _ in range(count):
        prices.append(prices[-1] + rng.gauss(0.0, 0.10))
    atr = 0.30
    rows: list[dict] = []
    for index, stamp in enumerate(stamps):
        move = prices[index + 24] - prices[index] if index + 24 < len(prices) else 0.0
        direction = rng.choice(["long", "short"])
        features = _features(rng)
        features["tf_agreement"] = rng.choice([0.25, 0.5, 0.75, 1.0])
        features["news_count"] = float(rng.randint(0, 6))
        features["rating_4h"] = rng.uniform(-1.0, 1.0)
        features["rating_1d"] = rng.uniform(-1.0, 1.0)
        if bad_cell:
            # rating_4h が売り寄りのときだけ、必ず順行しない方向を選ぶ。
            if features["rating_4h"] < -0.25:
                direction = "short" if move > 0 else "long"
        rows.append(
            {
                "ts": stamp.isoformat(),
                "prediction_time": stamp.isoformat(),
                "source_cutoff": (stamp - timedelta(minutes=2)).isoformat(),
                "max_feature_available_time": (stamp - timedelta(seconds=1)).isoformat(),
                "pit_eligible": True,
                "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
                "decision_id": f"decision:USDJPY:{stamp.isoformat()}",
                "mode": "fusion",
                "producer": journal.FUSION_PRODUCER,
                "producer_version": journal.FUSION_PRODUCER_VERSION,
                "input_context_id": f"context:{stamp.isoformat()}",
                "source_record_ids": [f"source:{stamp.isoformat()}"],
                "symbol": "USDJPY",
                "direction": direction,
                "conviction": rng.randint(20, 80),
                "composite": 0.4,
                "tech_score": rng.uniform(-1.0, 1.0),
                "news_score": rng.uniform(-1.0, 1.0),
                "close": round(prices[index], 4),
                "atr": atr,
                "data_quality": 0.9,
                "features": features,
            }
        )
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )


def test_audit_measures_live_tau_and_compares_with_the_artifact(tmp_path: Path) -> None:
    fusion = tmp_path / "briefing_journal.jsonl"
    _fusion_journal(fusion)
    report_path = tmp_path / "report.json"
    code = learning_prior_audit.main(
        [
            "--fusion-journal",
            str(fusion),
            "--tf-journal",
            str(tmp_path / "absent.jsonl"),
            "--tf-prices",
            str(tmp_path / "absent-prices.jsonl"),
            "--json",
            str(report_path),
        ]
    )
    assert code == 0  # コイン投げ相当の入力では履歴上限を超えない
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["prior_loaded"] is True
    slices = {entry["slice"]: entry for entry in report["slices"]}
    fusion_report = slices[learning_prior.FUSION_KEY]
    assert fusion_report["scored"] > 100
    assert fusion_report["overlap_factor"] == 24.0  # 24hラベル ÷ 1h間引き
    features = fusion_report["features"]
    # 履歴で測れた4件は artifact_tau を持ち、測れない4件は live_only になる
    assert features["rsi_1h"]["artifact_tau"] is not None
    assert features["rating_4h"]["artifact_tau"] is None
    assert features["rating_4h"]["verdict"] in {
        learning_prior_audit.VERDICT_LIVE_ONLY,
        learning_prior_audit.VERDICT_INCONCLUSIVE,
    }
    comparison = fusion_report["adjustment_comparison"]
    assert comparison["probes"] > 100
    # 収縮は減衰を増やさない
    assert comparison["decayed_share_after"] <= comparison["decayed_share_before"]
    assert fusion_report["fired_cells_after"] <= fusion_report["fired_cells_before"]


def test_audit_flags_dispersion_that_exceeds_the_historical_bound(tmp_path: Path) -> None:
    fusion = tmp_path / "briefing_journal.jsonl"
    _fusion_journal(fusion, bad_cell=True)
    code = learning_prior_audit.main(
        [
            "--fusion-journal",
            str(fusion),
            "--tf-journal",
            str(tmp_path / "absent.jsonl"),
            "--tf-prices",
            str(tmp_path / "absent-prices.jsonl"),
            "--json",
            str(tmp_path / "report.json"),
        ]
    )
    # rating_4h は履歴に無いので live_only。tau自体は大きく出るが exceeds にはしない。
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    fusion_report = report["slices"][0]
    rating = fusion_report["features"]["rating_4h"]
    assert rating["tau_hat"] is not None and float(rating["tau_hat"]) > 0.02
    assert code in (0, learning_prior_audit.EXIT_TAU_EXCEEDED)


def test_audit_emits_a_proposal_only_for_features_history_cannot_measure(tmp_path: Path) -> None:
    fusion = tmp_path / "briefing_journal.jsonl"
    _fusion_journal(fusion, bad_cell=True)
    proposed_path = tmp_path / "proposed.json"
    learning_prior_audit.main(
        [
            "--fusion-journal",
            str(fusion),
            "--tf-journal",
            str(tmp_path / "absent.jsonl"),
            "--tf-prices",
            str(tmp_path / "absent-prices.jsonl"),
            "--emit-artifact",
            str(proposed_path),
        ]
    )
    proposed = json.loads(proposed_path.read_text(encoding="utf-8"))
    committed = json.loads(COMMITTED_ARTIFACT.read_text(encoding="utf-8"))
    fusion_slice = proposed["slices"][learning_prior.FUSION_KEY]
    historical_cap = max(
        float(entry["tau_upper95"]) for entry in committed["slices"]["fusion"].values()
    )
    for feature, entry in fusion_slice.items():
        if feature in committed["slices"]["fusion"]:
            # 履歴で測れているものは書き換えない
            assert entry == committed["slices"]["fusion"][feature]
        else:
            assert entry["source"] == learning_prior.SOURCE_LIVE_JOURNAL
            if entry["tau_hat"] > 0.0:
                # 統計的に支持される差はキャップせず、実測をそのまま使う
                assert entry["tau_upper95"] == entry["measured_tau_upper95"]
                assert entry["capped_by_historical_max"] is None
            else:
                # 標本誤差だけの上限は履歴の最大upperで抑える
                assert entry["tau_upper95"] <= historical_cap + 1e-9
    assert proposed["live_audit"]["added"]
    # 提案版もそのまま読み込める契約であること
    assert learning_prior.prior_from_mapping(proposed) is not None


def test_audit_is_quiet_without_journals(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = learning_prior_audit.main(
        [
            "--fusion-journal",
            str(tmp_path / "a.jsonl"),
            "--tf-journal",
            str(tmp_path / "b.jsonl"),
            "--tf-prices",
            str(tmp_path / "c.jsonl"),
        ]
    )
    assert code == 0
    assert "ジャーナルが見つからない" in capsys.readouterr().out


def test_overlap_factor_matches_the_label_horizon() -> None:
    assert learning_prior_audit.overlap_factor(24.0) == 24.0
    assert learning_prior_audit.overlap_factor(1.0) == 1.0
    assert learning_prior_audit.overlap_factor(0.25) == 1.0  # 間引きより短い足は重ならない


# --- M5コーパス経路（tf:15m を含む） -----------------------------------------


def _write_m5_corpus(root: Path, pair: str, bars: int = 4000, seed: int = 4) -> None:
    """historical_chart と同じ配置の M5 bid/ask 月次ファイルを1本だけ作る。"""
    import gzip

    directory = root / pair
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    stamp = datetime(2024, 1, 1, 22, 0, tzinfo=UTC)
    mid = 150.0
    spread = 0.01
    lines = ["timestamp,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close"]
    for _ in range(bars):
        close = mid + rng.gauss(0.0, 0.02)
        high, low = max(mid, close) + 0.01, min(mid, close) - 0.01
        values = [mid, high, low, close]
        bid = [value - spread / 2 for value in values]
        ask = [value + spread / 2 for value in values]
        lines.append(stamp.isoformat() + "," + ",".join(f"{value:.5f}" for value in (*bid, *ask)))
        mid = close
        stamp += timedelta(minutes=5)
    path = directory / f"{pair}_2024_m5_bidask.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def test_m5_corpus_produces_the_15m_slice(tmp_path: Path) -> None:
    root = tmp_path / "bars_m5"
    _write_m5_corpus(root, "USDJPY")
    _write_m5_corpus(root, "EURUSD", seed=5)
    payload = build_learning_prior.build_payload(
        (), "m5-unit-test", corpus="m5", m5_root=root, m5_pairs=("USDJPY", "EURUSD")
    )
    slices = payload["slices"]
    assert isinstance(slices, dict)
    assert learning_prior.timeframe_key("15m") in slices
    assert learning_prior.FUSION_KEY in slices
    corpus = payload["corpus"]
    assert isinstance(corpus, dict)
    assert corpus["bar_interval_hours"] == pytest.approx(5 / 60, abs=1e-6)
    assert corpus["pairs"] == ["EURUSD", "USDJPY"]
    for dataset in corpus["datasets"]:
        assert dataset["files"] == 1
        assert dataset["bars"] > 0
    prior = learning_prior.prior_from_mapping(payload)
    assert prior is not None
    assert prior.feature(learning_prior.timeframe_key("15m"), "rsi_1h") is not None


def test_m5_corpus_reports_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_learning_prior.build_payload(
            (), "m5-unit-test", corpus="m5", m5_root=tmp_path / "absent", m5_pairs=("USDJPY",)
        )


def test_resample_keeps_source_bars_when_target_is_not_coarser() -> None:
    bars = [
        (datetime(2024, 1, 1, 22, 0, tzinfo=UTC) + timedelta(minutes=5 * i), 1.0, 2.0, 0.5, 1.5)
        for i in range(24)
    ]
    assert build_learning_prior.resample(bars, 5 / 60, 5 / 60) == bars
    quarter = build_learning_prior.resample(bars, 0.25, 5 / 60)
    assert len(quarter) == 8  # 24本のM5 -> 8本の15m
    assert quarter[0][2] == 2.0 and quarter[0][3] == 0.5  # high/low が集約されている


# --- 共有ルーチン: de-mean と負のゼロ ----------------------------------------


def _flat_bucketize(feature: str, value: float) -> str | None:
    return "high" if value >= 0.5 else "low"


def test_cell_deviations_removes_the_group_direction_baseline() -> None:
    """一方向だけ勝ち続けるグループがあっても、セル偏差には出ない。"""
    observations: list[tuple[str, str, dict[str, float], bool]] = []
    for index in range(400):
        # USDJPY の long は常に勝ち、short は常に負ける(強いトレンド期)
        observations.append(("USDJPY", "long", {"x": index % 2}, True))
        observations.append(("USDJPY", "short", {"x": index % 2}, False))
    measured = learning_prior.cell_deviations(observations, 1.0, ["x"], _flat_bucketize)
    assert set(measured["x"].detail) == {"low|long", "high|long", "low|short", "high|short"}
    # 方向の勝敗差は baseline に吸収され、セル偏差はゼロになる
    assert max(abs(value) for value in measured["x"].deviations) < 1e-9
    tau_hat, tau_upper, _q = learning_prior.dersimonian_laird(
        measured["x"].deviations, measured["x"].variances
    )
    assert tau_hat == 0.0 and tau_upper == 0.0


def test_cell_deviations_applies_the_overlap_factor() -> None:
    observations = [("USDJPY", "long", {"x": index % 2}, index % 3 == 0) for index in range(240)]
    dense = learning_prior.cell_deviations(observations, 1.0, ["x"], _flat_bucketize)["x"]
    sparse = learning_prior.cell_deviations(observations, 24.0, ["x"], _flat_bucketize)["x"]
    assert dense.deviations == sparse.deviations  # 偏差は同じ
    # 重複を数えれば実効nは1/24、分散は24倍
    assert sparse.effective[0] == pytest.approx(dense.effective[0] / 24.0)
    assert sparse.variances[0] == pytest.approx(dense.variances[0] * 24.0)


def _negative_zeros(node: object, path: str = "") -> list[str]:
    if isinstance(node, dict):
        return [
            item for key, value in node.items() for item in _negative_zeros(value, f"{path}/{key}")
        ]
    if isinstance(node, list):
        return [
            item
            for index, value in enumerate(node)
            for item in _negative_zeros(value, f"{path}[{index}]")
        ]
    if isinstance(node, float) and node == 0.0 and math.copysign(1.0, node) < 0:
        return [path]
    return []


def test_committed_artifact_has_no_negative_zero() -> None:
    """-0.0 が残ると、加算順序の違いだけで payload_sha256 がぶれる。"""
    payload = json.loads(COMMITTED_ARTIFACT.read_text(encoding="utf-8"))
    assert _negative_zeros(payload) == []


def test_cell_deviations_normalises_negative_zero() -> None:
    observations = [("USDJPY", "long", {"x": index % 2}, index % 2 == 0) for index in range(200)]
    measured = learning_prior.cell_deviations(observations, 1.0, ["x"], _flat_bucketize)
    for values in measured["x"].detail.values():
        assert math.copysign(1.0, values["deviation"]) > 0 or values["deviation"] != 0.0
