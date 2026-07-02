from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pandas as pd

import auto_optimize
import params_gate
import promote_params

REPO_ROOT = Path(params_gate.__file__).resolve().parent


def write_price_csv(path: Path, *, periods: int, freq: str = "h", symbol: str = "EURUSD") -> Path:
    """実データと同じ形式の決定的な価格CSVを生成する（サイン波+微トレンド）。"""
    index = pd.date_range("2024-01-01 00:00:00", periods=periods, freq=freq)
    rows = []
    prev_close = 1.09
    for i, ts in enumerate(index):
        close = 1.09 + 0.02 * math.sin(i / 25) + 0.000002 * i
        rows.append(
            {
                "timestamp": ts,
                "symbol": symbol,
                "open": round(prev_close, 6),
                "high": round(max(prev_close, close) + 0.0005, 6),
                "low": round(min(prev_close, close) - 0.0005, 6),
                "close": round(close, 6),
            }
        )
        prev_close = close
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def valid_params(**overrides) -> dict:
    params = {
        "fast_window": 20,
        "slow_window": 100,
        "atr_window": 14,
        "atr_multiple": 2.5,
        "best_symbol": "EURUSD",
        "score": 1.23,
        "sharpe": 1.1,
        "profit_factor": 1.5,
        "max_drawdown_pct": 2.0,
        "updated_at": "2026-07-02T00:00:00+00:00",
        "provenance": {
            "schema": params_gate.SCHEMA_VERSION,
            "generated_by": "auto_optimize.py",
            "data": {
                "path": "/data/real_prices.csv",
                "sha256": "ab" * 32,
                "rows": 5000,
                "start": "2025-01-01 00:00:00",
                "end": "2025-12-31 23:00:00",
            },
            "trade_count": 42,
            "oos": {"sharpe": 0.9, "trade_count": 12, "is_sharpe": 1.1},
            "warnings": [],
        },
    }
    params.update(overrides)
    return params


# ── validate_data_source ─────────────────────────────────────────────────────


def test_data_source_rejects_unset() -> None:
    for raw in (None, "", "   "):
        path, errors = params_gate.validate_data_source(raw)
        assert path is None
        assert errors


def test_data_source_rejects_missing_file(tmp_path: Path) -> None:
    path, errors = params_gate.validate_data_source(str(tmp_path / "nope.csv"))
    assert path is None
    assert any("存在しない" in e for e in errors)


def test_data_source_rejects_bundled_sample() -> None:
    path, errors = params_gate.validate_data_source(str(params_gate.BUNDLED_SAMPLE))
    assert path is None
    assert errors


def test_data_source_rejects_copied_sample_content(tmp_path: Path) -> None:
    copy = tmp_path / "innocent_looking.csv"
    shutil.copyfile(params_gate.BUNDLED_SAMPLE, copy)
    path, errors = params_gate.validate_data_source(str(copy))
    assert path is None
    assert any("合成データ" in e for e in errors)


def test_data_source_rejects_short_span(tmp_path: Path) -> None:
    csv = write_price_csv(tmp_path / "short.csv", periods=2000, freq="h")  # ~83日
    path, errors = params_gate.validate_data_source(str(csv))
    assert path is None
    assert any("期間が不足" in e for e in errors)


def test_data_source_rejects_too_few_rows(tmp_path: Path) -> None:
    csv = write_price_csv(tmp_path / "sparse.csv", periods=500, freq="12h")  # 250日
    path, errors = params_gate.validate_data_source(str(csv))
    assert path is None
    assert any("行数が不足" in e for e in errors)


def test_data_source_accepts_real_shaped_data(tmp_path: Path) -> None:
    csv = write_price_csv(tmp_path / "real.csv", periods=4500, freq="h")  # ~187日
    path, errors = params_gate.validate_data_source(str(csv))
    assert errors == []
    assert path == csv.resolve()


# ── validate_params / load_validated_params ─────────────────────────────────


def test_validate_params_accepts_valid() -> None:
    assert params_gate.validate_params(valid_params()) == []


def test_validate_params_rejects_missing_provenance() -> None:
    params = valid_params()
    del params["provenance"]
    errors = params_gate.validate_params(params)
    assert any("provenance" in e for e in errors)


def test_validate_params_rejects_legacy_active_file_shape() -> None:
    # リポジトリの旧 strategy_params.json と同じ形（provenance 無し）は拒否される
    legacy = {
        "fast_window": 20,
        "slow_window": 100,
        "atr_window": 14,
        "atr_multiple": 2.5,
        "best_symbol": "EURUSD",
        "score": 140911.9994,
        "sharpe": 3.9623,
        "updated_at": "2026-06-30T07:46:47.297995+00:00",
    }
    assert params_gate.validate_params(legacy)


def test_validate_params_rejects_inverted_windows() -> None:
    errors = params_gate.validate_params(valid_params(fast_window=100, slow_window=20))
    assert any("fast_window" in e for e in errors)


def test_validate_params_rejects_out_of_bounds() -> None:
    assert params_gate.validate_params(valid_params(atr_multiple=50.0))
    assert params_gate.validate_params(valid_params(fast_window=0))
    assert params_gate.validate_params(valid_params(slow_window="100"))


def test_validate_params_rejects_synthetic_data_hash() -> None:
    params = valid_params()
    params["provenance"]["data"]["sha256"] = next(iter(params_gate.KNOWN_SYNTHETIC_SHA256))
    errors = params_gate.validate_params(params)
    assert any("合成サンプル" in e for e in errors)


def test_validate_params_rejects_low_trade_count() -> None:
    params = valid_params()
    params["provenance"]["trade_count"] = 3
    errors = params_gate.validate_params(params)
    assert any("取引数が不足" in e for e in errors)


def test_load_validated_params_handles_bad_file(tmp_path: Path) -> None:
    missing, errors = params_gate.load_validated_params(tmp_path / "none.json")
    assert missing is None and errors

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    params, errors = params_gate.load_validated_params(broken)
    assert params is None and errors


# ── auto_optimize ────────────────────────────────────────────────────────────


def test_auto_optimize_refuses_without_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPTIMIZE_DATA", raising=False)
    out = tmp_path / "candidate.json"
    assert auto_optimize.main(["--output", str(out)]) == 1
    assert not out.exists()


def test_auto_optimize_refuses_sample_data(tmp_path: Path) -> None:
    out = tmp_path / "candidate.json"
    rc = auto_optimize.main(["--data", str(params_gate.BUNDLED_SAMPLE), "--output", str(out)])
    assert rc == 1
    assert not out.exists()


def test_auto_optimize_writes_candidate_with_provenance(tmp_path: Path, monkeypatch) -> None:
    csv = write_price_csv(tmp_path / "real.csv", periods=4500)
    out = tmp_path / "candidate.json"
    monkeypatch.setattr(auto_optimize, "FAST_WINDOWS", [10])
    monkeypatch.setattr(auto_optimize, "SLOW_WINDOWS", [40])
    monkeypatch.setattr(auto_optimize, "ATR_MULTS", [2.0])

    assert auto_optimize.main(["--data", str(csv), "--output", str(out)]) == 0

    candidate = json.loads(out.read_text())
    assert candidate["fast_window"] == 10
    assert candidate["slow_window"] == 40
    prov = candidate["provenance"]
    assert prov["data"]["sha256"] == params_gate.sha256_file(csv)
    assert prov["data"]["rows"] == 4500
    assert isinstance(prov["trade_count"], int)
    assert "oos" in prov and "warnings" in prov


# ── promote_params ───────────────────────────────────────────────────────────


def _write(path: Path, params: dict) -> None:
    path.write_text(json.dumps(params, indent=2, ensure_ascii=False))


def test_promote_backs_up_active_and_promotes(tmp_path: Path) -> None:
    candidate = valid_params()
    legacy_active = {"fast_window": 20, "slow_window": 100}
    _write(tmp_path / promote_params.CANDIDATE_NAME, candidate)
    _write(tmp_path / promote_params.ACTIVE_NAME, legacy_active)

    assert promote_params.promote(tmp_path) == 0
    active = json.loads((tmp_path / promote_params.ACTIVE_NAME).read_text())
    prev = json.loads((tmp_path / promote_params.PREV_NAME).read_text())
    assert active == candidate
    assert prev == legacy_active


def test_promote_refuses_invalid_candidate(tmp_path: Path) -> None:
    bad = valid_params()
    del bad["provenance"]
    legacy_active = {"fast_window": 20, "slow_window": 100}
    _write(tmp_path / promote_params.CANDIDATE_NAME, bad)
    _write(tmp_path / promote_params.ACTIVE_NAME, legacy_active)

    assert promote_params.promote(tmp_path) == 1
    active = json.loads((tmp_path / promote_params.ACTIVE_NAME).read_text())
    assert active == legacy_active
    assert not (tmp_path / promote_params.PREV_NAME).exists()


def test_promote_refuses_warned_candidate_without_force(tmp_path: Path) -> None:
    warned = valid_params()
    warned["provenance"]["warnings"] = ["overfit警告: OOS sharpe が非正 (-0.2)"]
    _write(tmp_path / promote_params.CANDIDATE_NAME, warned)

    assert promote_params.promote(tmp_path) == 1
    assert not (tmp_path / promote_params.ACTIVE_NAME).exists()
    assert promote_params.promote(tmp_path, force=True) == 0
    assert (tmp_path / promote_params.ACTIVE_NAME).exists()


def test_promote_check_only_changes_nothing(tmp_path: Path) -> None:
    _write(tmp_path / promote_params.CANDIDATE_NAME, valid_params())
    assert promote_params.promote(tmp_path, check_only=True) == 0
    assert not (tmp_path / promote_params.ACTIVE_NAME).exists()


def test_rollback_restores_previous(tmp_path: Path) -> None:
    old = valid_params(fast_window=15)
    new = valid_params(fast_window=25)
    _write(tmp_path / promote_params.CANDIDATE_NAME, new)
    _write(tmp_path / promote_params.ACTIVE_NAME, old)
    assert promote_params.promote(tmp_path) == 0

    assert promote_params.rollback(tmp_path) == 0
    active = json.loads((tmp_path / promote_params.ACTIVE_NAME).read_text())
    prev = json.loads((tmp_path / promote_params.PREV_NAME).read_text())
    assert active == old
    assert prev == new  # もう一度 rollback すれば new に戻れる


def test_rollback_requires_force_for_legacy_prev(tmp_path: Path) -> None:
    legacy_prev = {"fast_window": 20, "slow_window": 100}
    _write(tmp_path / promote_params.PREV_NAME, legacy_prev)
    _write(tmp_path / promote_params.ACTIVE_NAME, valid_params())

    assert promote_params.rollback(tmp_path) == 1
    assert promote_params.rollback(tmp_path, force=True) == 0
    active = json.loads((tmp_path / promote_params.ACTIVE_NAME).read_text())
    assert active == legacy_prev
