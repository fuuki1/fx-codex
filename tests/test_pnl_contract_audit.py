"""正準損益の恒等式と二重控除防止の契約テスト。ネットワーク不要。

⚠️ ここで数式を再実装しない。`canonical_net_label_contract_flags` を唯一の正とし、
「契約が破れた入力を正しく検出できるか」だけを固定する。
"""

from __future__ import annotations

import json
from pathlib import Path

from fx_intel.evaluation_labels import (
    FULL_COST_NET_LABEL_VERSION,
    NET_LABEL_VERSION,
    PROMOTION_NET_LABEL_VERSIONS,
    canonical_net_label_contract_flags,
)
from tools import pnl_contract_audit

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "canonical_outcome_full_cost.json"


def _full_cost_record() -> dict[str, object]:
    """契約を満たす実在の full-cost レコード。

    契約関数は損益の数式だけでなく、識別子・来歴・気配・ハッシュなど
    50項目以上を検査する。手組みのfixtureでは「数式は正しいが契約は通らない」
    状態になり、恒等式の検証にならない。そのため実機の canonical outcome から
    契約flagが空の1件を取り込み、それを基点にする。
    """
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_fixture_is_contract_clean() -> None:
    """基点fixtureが契約flag無しであること(以降のテストの前提)。"""
    assert canonical_net_label_contract_flags(_full_cost_record()) == ()


def test_r_space_identities_are_enforced_by_the_contract() -> None:
    """R空間の恒等式が壊れたら契約関数が検出する。"""
    base = _full_cost_record()

    # realized_net_r = quote_realized_r - additional_cost_r
    broken = {**base, "realized_net_r": base["realized_net_r"] + 0.5}  # type: ignore[operator]
    assert "net_r_identity_mismatch" in canonical_net_label_contract_flags(broken)

    # execution_cost_r = gross_realized_r - realized_net_r
    broken = {**base, "execution_cost_r": 999.0}
    assert "execution_cost_identity_mismatch" in canonical_net_label_contract_flags(broken)

    # additional_cost_r = slippage + commission + financing + conversion
    broken = {**base, "additional_cost_r": base["additional_cost_r"] + 0.5}  # type: ignore[operator]
    assert "additional_cost_identity_mismatch" in canonical_net_label_contract_flags(broken)


def test_spread_is_not_deducted_twice_from_net_r() -> None:
    """spreadを二重控除した値は契約に通らない。

    `quote_realized_r` は既に spread を引いた後の値。ここからさらに
    `realized_spread_cost_r` を引いて net を作ると恒等式が壊れる。
    """
    base = _full_cost_record()
    double_deducted = base["realized_net_r"] - base["realized_spread_cost_r"]  # type: ignore[operator]
    broken = {**base, "realized_net_r": double_deducted}

    flags = canonical_net_label_contract_flags(broken)
    assert "net_r_identity_mismatch" in flags


def test_execution_cost_r_already_includes_spread_in_full_cost() -> None:
    """full-costでは execution_cost_r = spread + additional。spreadを外すと検出される。"""
    base = _full_cost_record()
    without_spread = {**base, "execution_cost_r": base["additional_cost_r"]}

    flags = canonical_net_label_contract_flags(without_spread)
    assert "full_cost_identity_mismatch" in flags or "execution_cost_identity_mismatch" in flags


def test_entry_spread_r_is_diagnostic_and_not_part_of_net_identity() -> None:
    """entry_spread_r は損益の恒等式に現れない(診断値)。

    値を変えても net/execution_cost の恒等式は壊れない。壊れるなら
    どこかで再控除している証拠になる。
    """
    base = _full_cost_record()
    with_entry_spread = {
        **base,
        "entry_bid": 100.0,
        "entry_ask": 100.1,
        "planned_risk_distance": 1.0,
        "entry_spread_r": 0.1,
    }
    flags = canonical_net_label_contract_flags(with_entry_spread)
    assert "net_r_identity_mismatch" not in flags
    assert "execution_cost_identity_mismatch" not in flags


def test_jpy_identities_are_enforced() -> None:
    """JPY空間の恒等式が壊れたら検出する。"""
    base = _full_cost_record()

    broken = {**base, "executable_pnl_jpy": int(base["executable_pnl_jpy"]) + 100}  # type: ignore[arg-type]
    assert "executable_jpy_identity_mismatch" in canonical_net_label_contract_flags(broken)

    broken = {**base, "net_pnl_jpy": int(base["net_pnl_jpy"]) + 100}  # type: ignore[arg-type]
    assert "net_jpy_identity_mismatch" in canonical_net_label_contract_flags(broken)


def test_full_cost_label_must_stay_research_only() -> None:
    """net-r-v3 は promotion 対象にしてはいけない。"""
    assert FULL_COST_NET_LABEL_VERSION not in PROMOTION_NET_LABEL_VERSIONS
    assert NET_LABEL_VERSION in PROMOTION_NET_LABEL_VERSIONS

    base = _full_cost_record()
    promoted = {**base, "promotion_eligible": True, "research_only": False}
    flags = canonical_net_label_contract_flags(promoted)
    assert "full_cost_label_not_research_only" in flags


def test_audit_tool_is_read_only_and_refuses_to_overwrite(tmp_path) -> None:
    """監査ツールは source を変更せず、既存の出力も上書きしない。"""
    source = tmp_path / "outcomes.jsonl"
    record = _full_cost_record()
    source.write_text(json.dumps({"outcome": record}) + "\n", encoding="utf-8")
    before = source.read_bytes()

    output = tmp_path / "report.json"
    assert (
        pnl_contract_audit.main([str(source), "--record-path", "outcome", "--output", str(output)])
        == 0
    )
    assert source.read_bytes() == before, "source data が変更された"

    # 既存の出力は上書きせず、非ゼロで終了する
    assert (
        pnl_contract_audit.main([str(source), "--record-path", "outcome", "--output", str(output)])
        == 2
    )


def test_audit_reports_zero_identity_violations_for_a_clean_record(tmp_path) -> None:
    source = tmp_path / "outcomes.jsonl"
    source.write_text(json.dumps({"outcome": _full_cost_record()}) + "\n", encoding="utf-8")
    output = tmp_path / "report.json"
    pnl_contract_audit.main([str(source), "--record-path", "outcome", "--output", str(output)])

    report = json.loads(Path(output).read_text(encoding="utf-8"))
    identity = report["identity_audit"]
    assert identity["records_with_label_version"] == 1
    assert identity["contract_flagged"] == 0
    assert report["sources"][0]["sha256"]


def test_audit_does_not_zero_fill_missing_or_non_finite_values(tmp_path) -> None:
    """欠損・非有限をゼロで埋めず、件数として残す。"""
    source = tmp_path / "outcomes.jsonl"
    partial = {**_full_cost_record()}
    partial["slippage_r"] = None
    source.write_text(json.dumps({"outcome": partial}) + "\n", encoding="utf-8")
    output = tmp_path / "report.json"
    pnl_contract_audit.main([str(source), "--record-path", "outcome", "--output", str(output)])

    report = json.loads(Path(output).read_text(encoding="utf-8"))
    slippage = report["field_summary"]["slippage_r"]
    assert slippage["non_null"] == 0
    assert slippage["distribution"]["count"] == 0
