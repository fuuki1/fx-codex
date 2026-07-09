"""Phase 6: レポート集約と過剰最適化検定の検証(ネットワーク非依存)。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from dukascopy_cftc_model.config import PipelineConfig
from dukascopy_cftc_model.quality import QualityReport
from dukascopy_cftc_model.report import build_report
from dukascopy_cftc_model.walk_forward import run_walk_forward


def _wf_result():
    rng = np.random.default_rng(2)
    n = 4000
    idx = pd.date_range("2022-01-01", periods=n, freq="1h", tz="UTC")
    X = pd.DataFrame(rng.normal(0, 1, (n, 5)), columns=[f"f{i}" for i in range(5)], index=idx)
    beta = np.array([1.0, -0.8, 0.5, 0.0, 0.0])
    fret = pd.Series(
        (2.0 * (X.to_numpy() @ beta) + rng.normal(0, 1, n)) * 1e-4,
        index=idx,
        name="future_return",
    )
    cfg = PipelineConfig().with_walk_forward(
        train_bars=1500, test_bars=400, purge_bars=5, embargo_bars=5
    )
    return run_walk_forward(X, fret, fret, cfg)


def _quality() -> QualityReport:
    return QualityReport(
        price_bars=4000,
        price_warnings=[],
        cot_warnings={"EUR": [], "USD": []},
        price_coverage=0.95,
        cot_coverage=0.9,
    )


def test_build_report_has_all_required_outputs() -> None:
    wf = _wf_result()
    report = build_report(
        "EURUSD", "H1", 24, wf, _quality(), {"period": "2022-2024", "samples_used": 4000}
    )
    d = report.to_dict()
    # 最終出力の必須項目: 期待値・勝率・DD・PF・Sharpe・特徴量寄与
    m = d["metrics"]
    for key in ("expectancy_usd", "win_rate", "max_drawdown_pct", "profit_factor", "sharpe_ratio"):
        assert key in m
    assert d["feature_importance"]
    assert d["feature_importance"][0]["feature"] in {"f0", "f1"}  # 最も効く特徴量
    assert d["folds"]
    assert d["quality"]["coverage"] > 0
    assert "provenance" in d


def test_report_summary_is_human_readable() -> None:
    wf = _wf_result()
    report = build_report("EURUSD", "H1", 24, wf, _quality(), {"period": "2022-2024"})
    text = report.summary()
    assert "勝率" in text
    assert "Sharpe" in text
    assert "特徴量寄与" in text
    assert "EURUSD" in text


def test_overfitting_assessment_present_or_noted() -> None:
    wf = _wf_result()
    report = build_report("EURUSD", "H1", 24, wf, _quality(), {})
    over = report.to_dict()["overfitting"]
    # DSRが計算できるか、できなければnoteが付く(どちらかは必ず)
    assert "dsr" in over or "note" in over


def test_report_json_serializable() -> None:
    import json

    wf = _wf_result()
    report = build_report("EURUSD", "H1", 24, wf, _quality(), {"period": "x"})
    # JSONに落とせる(tupleやnp型が残っていないこと)
    json.dumps(report.to_dict(), ensure_ascii=False)
