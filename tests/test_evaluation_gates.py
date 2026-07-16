"""Evaluation gates: policy config, baseline dominance, sample-size guards."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fx_backtester.experiment_manifest import ModelCandidate, parse_experiment_manifest
from fx_backtester.experiment_pipeline import (
    GitState,
    _sample_size_guard,
    _select_trial,
    run_experiment,
)
from fx_backtester.failures import FailureReason, TypedFailure
from fx_backtester.promotion_policy import parse_promotion_policy

from test_experiment_pipeline import COMMIT, _manifest_dict, _write_prices

POLICY_PAYLOAD: dict[str, Any] = {
    "min_net_expectancy_r": 0.0,
    "min_expectancy_ci_lower_r": 0.0,
    "min_dsr_probability": 0.95,
    "max_pbo_probability": 0.2,
    "min_samples": 200,
    "min_regimes": 3,
    "min_pairs": 3,
    "max_drawdown_pct": 0.15,
    "min_brier_improvement": 0.0,
    "min_cost_stress_2x_expectancy_r": 0.0,
    "min_shadow_days_for_paper": 30,
    "min_paper_days_for_limited_live": 60,
    "allow_limited_live": False,
    "allow_live": False,
    "rationale": "Conservative research defaults restated for this test mandate.",
}


class TestPromotionPolicyConfig:
    def test_valid_policy_parses(self) -> None:
        policy = parse_promotion_policy(POLICY_PAYLOAD)
        assert policy.min_samples == 200
        assert policy.rationale.startswith("Conservative")

    def test_unknown_key_rejected(self) -> None:
        payload = {**POLICY_PAYLOAD, "surprise": 1}
        with pytest.raises(TypedFailure) as excinfo:
            parse_promotion_policy(payload)
        assert excinfo.value.reason is FailureReason.INVALID

    def test_missing_key_rejected(self) -> None:
        payload = dict(POLICY_PAYLOAD)
        del payload["min_dsr_probability"]
        with pytest.raises(TypedFailure) as excinfo:
            parse_promotion_policy(payload)
        assert excinfo.value.reason is FailureReason.INCOMPLETE

    def test_short_rationale_rejected(self) -> None:
        payload = {**POLICY_PAYLOAD, "rationale": "because"}
        with pytest.raises(TypedFailure):
            parse_promotion_policy(payload)

    def test_live_enablement_rejected(self) -> None:
        payload = {**POLICY_PAYLOAD, "allow_live": True}
        with pytest.raises(TypedFailure) as excinfo:
            parse_promotion_policy(payload)
        assert excinfo.value.reason is FailureReason.PROMOTION_REJECTED


def _parsed_manifest(**overrides: Any) -> Any:
    payload = _manifest_dict(Path("prices.csv"), "0" * 64, **overrides)
    return parse_experiment_manifest(payload)


def _fake_trial(candidate_id: str, family: str, trades: int, expectancy: float) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "candidate": ModelCandidate(
            candidate_id=candidate_id,
            family=family,
            hyperparameters={"long_threshold": 0.55, "short_threshold": 0.45},
        ),
        "tune_metrics": {"trade_count": trades, "net_expectancy_r": expectancy},
    }


class TestBaselineDominance:
    def test_baseline_wins_when_complex_does_not_beat_it(self) -> None:
        manifest = _parsed_manifest()
        trials = [
            _fake_trial("base", "rsi_reversion", 20, 0.05),
            _fake_trial("clever", "gbdt", 20, 0.05),
        ]
        selected = _select_trial(manifest, trials)
        assert selected["candidate_id"] == "base"

    def test_complex_selected_only_when_strictly_better(self) -> None:
        manifest = _parsed_manifest()
        trials = [
            _fake_trial("base", "rsi_reversion", 20, 0.05),
            _fake_trial("clever", "gbdt", 20, 0.08),
        ]
        assert _select_trial(manifest, trials)["candidate_id"] == "clever"

    def test_no_admissible_candidate_fails_closed(self) -> None:
        manifest = _parsed_manifest()
        trials = [_fake_trial("clever", "gbdt", 20, -0.01)]
        with pytest.raises(TypedFailure) as excinfo:
            _select_trial(manifest, trials)
        assert excinfo.value.reason is FailureReason.INVALID


class TestSampleSizeGuard:
    def _trade(self, when: str, bars: int = 2) -> dict[str, Any]:
        return {"prediction_time": when, "bars_to_exit": bars, "net_r": 0.1, "side": "long"}

    def test_overlapping_trades_shrink_effective_count(self) -> None:
        manifest = _parsed_manifest()
        trades = [self._trade(f"2024-01-02T0{h}:00:00+00:00", bars=10) for h in range(5)]
        guard = _sample_size_guard(manifest, trades, ["low_vol"] * 5)
        assert guard["test_trade_count"] == 5
        assert guard["effective_trade_count"] == 1
        assert guard["checks"]["effective_trades"]["passed"] is False

    def test_regime_concentration_detected(self) -> None:
        manifest = _parsed_manifest(
            selection={
                "primary_metric": "net_expectancy_r",
                "minimum_trade_count": 2,
                "minimum_effective_trades": 1,
                "max_regime_concentration": 0.5,
                "max_month_concentration": 1.0,
                "multiple_testing_method": "holm",
                "bootstrap_block_size": 5,
                "pbo_blocks": 8,
            }
        )
        trades = [self._trade(f"2024-01-0{d}T00:00:00+00:00") for d in range(1, 5)]
        guard = _sample_size_guard(manifest, trades, ["high_vol"] * 4)
        assert guard["checks"]["regime_concentration"]["passed"] is False
        assert guard["passed"] is False

    def test_month_concentration_detected(self) -> None:
        manifest = _parsed_manifest(
            selection={
                "primary_metric": "net_expectancy_r",
                "minimum_trade_count": 2,
                "minimum_effective_trades": 1,
                "max_regime_concentration": 1.0,
                "max_month_concentration": 0.5,
                "multiple_testing_method": "holm",
                "bootstrap_block_size": 5,
                "pbo_blocks": 8,
            }
        )
        trades = [self._trade(f"2024-01-0{d}T00:00:00+00:00") for d in range(1, 5)]
        guard = _sample_size_guard(manifest, trades, ["low_vol", "mid_vol", "high_vol", "low_vol"])
        assert guard["checks"]["month_concentration"]["passed"] is False

    def test_zero_trades_fails_every_check(self) -> None:
        manifest = _parsed_manifest()
        guard = _sample_size_guard(manifest, [], [])
        assert guard["passed"] is False


@pytest.fixture()
def full_setup(tmp_path: Path) -> dict[str, Any]:
    csv_path = tmp_path / "USDJPY.csv"
    csv_sha = _write_prices(csv_path)
    return {"tmp_path": tmp_path, "csv_path": csv_path, "csv_sha": csv_sha}


def _run_with(setup: dict[str, Any], payload: dict[str, Any], output: str = "out") -> Any:
    manifest_path = setup["tmp_path"] / f"{output}-manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return run_experiment(
        manifest_path,
        output_root=setup["tmp_path"] / output,
        repository_root=setup["tmp_path"],
        git_state=GitState(commit=COMMIT, dirty=False),
    )


class TestPipelineIntegration:
    def test_baseline_family_is_mandatory(self, full_setup: dict[str, Any]) -> None:
        payload = _manifest_dict(full_setup["csv_path"], full_setup["csv_sha"])
        payload["models"]["candidates"] = [
            candidate
            for candidate in payload["models"]["candidates"]
            if candidate["family"] == "logistic_ridge"
        ]
        with pytest.raises(TypedFailure) as excinfo:
            _run_with(full_setup, payload)
        assert excinfo.value.reason is FailureReason.INVALID

    def test_full_benchmark_family_runs_and_denies_promotion(
        self, full_setup: dict[str, Any]
    ) -> None:
        payload = _manifest_dict(full_setup["csv_path"], full_setup["csv_sha"])
        thresholds = {"long_threshold": 0.52, "short_threshold": 0.48}
        payload["models"]["candidates"] = [
            {
                "candidate_id": "noskill",
                "family": "constant_probability",
                "hyperparameters": {"probability": 0.5, **thresholds},
            },
            {"candidate_id": "random", "family": "random_uniform", "hyperparameters": thresholds},
            {"candidate_id": "long", "family": "always_long", "hyperparameters": thresholds},
            {"candidate_id": "short", "family": "always_short", "hyperparameters": thresholds},
            {
                "candidate_id": "prev-sign",
                "family": "previous_return_sign",
                "hyperparameters": {"strength": 0.1, **thresholds},
            },
            {
                "candidate_id": "ma",
                "family": "ma_crossover",
                "hyperparameters": {"strength": 0.1, **thresholds},
            },
            {
                "candidate_id": "rsi",
                "family": "rsi_reversion",
                "hyperparameters": {"strength": 0.1, **thresholds},
            },
            {
                "candidate_id": "logit",
                "family": "logistic_ridge",
                "hyperparameters": {"ridge": 1.0, **thresholds},
            },
            {
                "candidate_id": "ridge-reg",
                "family": "ridge_regression",
                "hyperparameters": {"ridge": 1.0, **thresholds},
            },
            {
                "candidate_id": "gbdt",
                "family": "gbdt",
                "hyperparameters": {
                    "n_estimators": 10,
                    "learning_rate": 0.1,
                    "max_depth": 2,
                    "min_samples_leaf": 10,
                    "subsample": 1.0,
                    "feature_fraction": 1.0,
                    "reg_lambda": 1.0,
                    **thresholds,
                },
            },
        ]
        payload["models"]["trial_budget"] = 10
        result = _run_with(full_setup, payload)
        assert result.promotion_passed is False
        decision = json.loads((result.output_dir / "promotion_decision.json").read_text("utf-8"))
        assert decision["evidence"]["trial_count"] == 10
        ledger_lines = (
            (result.output_dir / "trial_ledger_snapshot.jsonl").read_text("utf-8").splitlines()
        )
        assert len(ledger_lines) == 10

    def test_policy_file_is_wired_into_the_decision(self, full_setup: dict[str, Any]) -> None:
        policy_path = full_setup["tmp_path"] / "policy.json"
        policy_path.write_text(
            json.dumps({**POLICY_PAYLOAD, "min_samples": 12345}), encoding="utf-8"
        )
        payload = _manifest_dict(full_setup["csv_path"], full_setup["csv_sha"])
        payload["promotion"]["policy_path"] = str(policy_path)
        result = _run_with(full_setup, payload)
        decision = json.loads((result.output_dir / "promotion_decision.json").read_text("utf-8"))
        assert decision["policy_source"] == str(policy_path)
        gates = {gate["name"]: gate for gate in decision["report"]["gates"]}
        assert gates["sample_size"]["requirement"] == "sample_count >= 12345"
        assert gates["sample_size"]["passed"] is False

    def test_strict_month_concentration_makes_evaluation_unavailable(
        self, full_setup: dict[str, Any]
    ) -> None:
        payload = _manifest_dict(full_setup["csv_path"], full_setup["csv_sha"])
        payload["selection"]["max_month_concentration"] = 0.1
        result = _run_with(full_setup, payload)
        evaluation = json.loads((result.output_dir / "evaluation.json").read_text("utf-8"))
        assert evaluation["performance_claim"] == "evaluation_unavailable"
        assert evaluation["sample_size_guard"]["checks"]["month_concentration"]["passed"] is False
