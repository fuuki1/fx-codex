from __future__ import annotations

from pathlib import Path

from fx_intel.virtual_portfolio import open_virtual_portfolio_writer
from tests.test_virtual_portfolio import _initialized
from tests.test_virtual_portfolio_v2 import _valuation
import tools.virtual_portfolio_readiness as readiness
from tools.virtual_portfolio_learning import _create_artifacts


def test_readiness_reads_only_one_completed_hash_valid_learning_run(tmp_path) -> None:
    run_root = tmp_path / "learning"
    manifest = {
        "generated_at": "2026-07-31T00:00:00+00:00",
        "source_dataset_sha256": "a" * 64,
    }
    target, *_ = _create_artifacts(
        run_root,
        manifest,
        {"memory": True},
        {"evidence": True},
        {"promotion": True},
        {"model": True},
    )
    completed = readiness._latest_completed_learning_run(run_root)

    assert completed is not None
    run_id, payloads = completed
    assert run_id == target.parent.name
    assert payloads["challenger_gate.json"] == manifest
    assert set(payloads) == set(readiness.LEARNING_ARTIFACT_NAMES)

    target.chmod(0o644)
    target.write_text('{"tampered":true}\n', encoding="utf-8")
    assert readiness._latest_completed_learning_run(run_root) is None


def test_readiness_p2_requires_strengthened_identity_and_baseline_contract(
    tmp_path,
    monkeypatch,
) -> None:
    database, policy = _initialized(tmp_path)
    with open_virtual_portfolio_writer(database, writer_id="test") as store:
        store.record_synchronized_liquidation_valuation(
            _valuation(policy, (), valuation_id="readiness-valuation")
        )

    def fake_json_url(url: str) -> dict[str, object]:
        if url.endswith("/health"):
            return {"ok": True, "read_only": True}
        return {
            "configured": True,
            "balance_jpy": 1_000_000,
            "equity_jpy": 1_000_000,
        }

    monkeypatch.setattr(readiness, "_json_url", fake_json_url)
    root = Path(__file__).resolve().parents[1]
    payload = readiness.audit(
        root=root,
        db=database,
        read_api_url="http://127.0.0.1:8771",
        dashboard_url="http://127.0.0.1:8788",
        check_launchd=False,
    )
    stages = payload["stages"]
    p1 = {check["key"]: check for check in stages["P1"]["checks"]}
    p2 = {check["key"]: check for check in stages["P2"]["checks"]}

    assert p1["schema_v4"]["passed"] is True
    assert p1["append_only_enforcement"]["passed"] is True
    assert p2["v2_identity_audit_contract_presence"]["passed"] is True
    assert p2["pit_period_baseline_contract_presence"]["passed"] is True
    assert p2["checkpoint_tooling_contract_presence"]["passed"] is True
    assert p2["analysis_only_execution"]["passed"] is True
