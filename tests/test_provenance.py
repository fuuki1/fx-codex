"""判断の最小限の来歴(code_hash / config_hash)のテスト。ネットワーク不要。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from fx_intel import journal, provenance

NOW = datetime(2026, 8, 5, tzinfo=UTC)


class _Plan:
    input_context_id = "ctx-1"
    source_record_ids = ("src-1",)


def _envelope() -> dict[str, object]:
    return journal.pit_metadata_for_plan(
        _Plan(),
        prediction_time=NOW,
        source_cutoff=NOW - timedelta(minutes=2),
        max_feature_available_time=NOW - timedelta(seconds=1),
        decision_id="decision-1",
        mode="fusion",
    )


def test_decision_envelope_carries_code_and_config_hash() -> None:
    """判断行は「どのコード・どの設定で出たか」を必ず持つ。

    decision_id / input_context_id / source_record_ids は「どの入力から」を
    示すが、それだけでは再計算しても同じ判断になる保証がない。
    """
    envelope = _envelope()

    assert envelope["provenance_contract"] == provenance.PROVENANCE_CONTRACT
    for key in ("code_hash", "config_hash"):
        value = envelope[key]
        assert isinstance(value, str)
        assert len(value) == 64, f"{key} はsha256(64桁)でなければならない"
        int(value, 16)  # 16進として解釈できること


def test_code_hash_changes_when_decision_code_changes(tmp_path, monkeypatch) -> None:
    """判断を決めるモジュールが変わったら code_hash も変わる。

    変わらなければ「別のコードで出た判断」を同一視してしまい、
    再現性の証明にならない。
    """
    baseline = provenance.code_hash()

    # 判断モジュールの1つを差し替えた状態を再現する
    fake_root = tmp_path / "fx_intel"
    fake_root.mkdir()
    for name in provenance.DECISION_CODE_MODULES:
        (fake_root / name).write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(provenance, "_MODULE_ROOT", fake_root)
    provenance.code_hash.cache_clear()
    stubbed = provenance.code_hash()
    assert stubbed != baseline

    # 1ファイルだけ内容を変えても検出される
    (fake_root / provenance.DECISION_CODE_MODULES[0]).write_text(
        "# stub changed\n", encoding="utf-8"
    )
    provenance.code_hash.cache_clear()
    assert provenance.code_hash() != stubbed

    monkeypatch.undo()
    provenance.code_hash.cache_clear()
    assert provenance.code_hash() == baseline


def test_config_hash_changes_when_decision_parameters_change() -> None:
    """判断を左右する設定が変われば config_hash も変わる。"""
    base_config = provenance.default_decision_config()
    baseline = provenance.config_hash(base_config)

    assert provenance.config_hash(base_config) == baseline  # 同じ入力なら安定
    for key in ("tech_weight", "conflict_threshold", "horizon_hours"):
        altered = {**base_config, key: base_config[key] + 0.01}
        assert provenance.config_hash(altered) != baseline, f"{key} の変化を検出できない"


def test_code_hash_survives_unreadable_module(tmp_path, monkeypatch) -> None:
    """モジュールが読めなくても判断は継続できる(来歴は値の違いとして残る)。"""
    fake_root = tmp_path / "fx_intel"
    fake_root.mkdir()
    for name in provenance.DECISION_CODE_MODULES:
        (fake_root / name).write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(provenance, "_MODULE_ROOT", fake_root)
    provenance.code_hash.cache_clear()
    complete = provenance.code_hash()

    (fake_root / provenance.DECISION_CODE_MODULES[0]).unlink()
    provenance.code_hash.cache_clear()
    missing = provenance.code_hash()

    assert isinstance(missing, str) and len(missing) == 64
    assert missing != complete, "欠落が来歴に反映されていない"


def test_scoring_modules_are_not_part_of_code_hash() -> None:
    """採点・学習側の変更で過去判断の code_hash が変わってはいけない。

    採点は事後評価であり判断そのものではない。含めてしまうと採点ロジックを
    触るたびに「判断が変わった」ように見え、再現性の議論ができなくなる。
    """
    for name in ("learning.py", "trade_outcome.py", "virtual_portfolio.py"):
        assert name not in provenance.DECISION_CODE_MODULES


@pytest.mark.parametrize("mode", ["fusion", "per_timeframe"])
def test_provenance_is_attached_for_every_decision_mode(mode: str) -> None:
    envelope = journal.pit_metadata_for_plan(
        _Plan(),
        prediction_time=NOW,
        source_cutoff=NOW - timedelta(minutes=2),
        max_feature_available_time=NOW - timedelta(seconds=1),
        decision_id="decision-1",
        mode=mode,
    )
    assert envelope["code_hash"] == provenance.code_hash()
    assert envelope["config_hash"] == provenance.config_hash()
