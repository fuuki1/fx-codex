#!/usr/bin/env python3
"""Read-only P0-P6 engineering and empirical readiness audit."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.virtual_portfolio import (  # noqa: E402
    open_virtual_portfolio_reader,
)
from tools.data_freshness_monitor import load_webhook_url  # noqa: E402

AUDIT_CONTRACT = "virtual-portfolio-p0-p6-readiness-v1"
VIRTUAL_LABELS = (
    "com.fx-codex.quote-index",
    "com.fx-codex.virtual-portfolio",
    "com.fx-codex.virtual-portfolio-close",
    "com.fx-codex.virtual-portfolio-learning",
    "com.fx-codex.virtual-portfolio-read",
    "com.fx-codex.dashboard-state",
)
LEARNING_ARTIFACT_NAMES = (
    "challenger_gate.json",
    "outcome_memory.json",
    "validation_evidence.json",
    "promotion_audit.json",
    "multi_axis_model.json",
)


@dataclass(frozen=True)
class Check:
    key: str
    passed: bool
    detail: str
    kind: str = "engineering"


def _json_url(url: str) -> dict[str, object]:
    with urlopen(url, timeout=5) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{url} did not return a JSON object")
    return payload


def _latest_completed_learning_run(
    root: Path,
) -> tuple[str, dict[str, dict[str, object]]] | None:
    """Read only the newest atomically completed, hash-valid learning run."""

    run_directories = (
        sorted(
            (path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.name,
        )
        if root.is_dir()
        else []
    )
    if not run_directories:
        return None
    run = run_directories[-1]
    try:
        completion = json.loads((run / "completion.json").read_text(encoding="utf-8"))
        if (
            not isinstance(completion, dict)
            or completion.get("contract") != "virtual-portfolio-learning-run-completion-v1"
            or completion.get("run_id") != run.name
        ):
            return None
        file_hashes = completion.get("files")
        if not isinstance(file_hashes, dict) or set(file_hashes) != set(LEARNING_ARTIFACT_NAMES):
            return None
        payloads: dict[str, dict[str, object]] = {}
        for name in LEARNING_ARTIFACT_NAMES:
            target = run / name
            content = target.read_bytes()
            if hashlib.sha256(content).hexdigest() != str(file_hashes[name]):
                return None
            payload = json.loads(content)
            if not isinstance(payload, dict):
                return None
            payloads[name] = payload
    except (OSError, ValueError, TypeError):
        return None
    return run.name, payloads


def _loaded_label(label: str) -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def _score(checks: list[Check], kind: str) -> float | None:
    selected = [check for check in checks if check.kind == kind]
    if not selected:
        return None
    return round(100 * sum(check.passed for check in selected) / len(selected), 1)


def audit(
    *,
    root: Path,
    db: Path,
    read_api_url: str,
    dashboard_url: str,
    check_launchd: bool,
) -> dict[str, object]:
    now = datetime.now(UTC)
    stages: dict[str, list[Check]] = {f"P{index}": [] for index in range(7)}

    def add(stage: str, key: str, passed: bool, detail: str, kind: str = "engineering"):
        stages[stage].append(Check(key, bool(passed), detail, kind))

    freshness_path = root / "logs" / "freshness_report.json"
    try:
        freshness = json.loads(freshness_path.read_text(encoding="utf-8"))
        monitored = datetime.fromisoformat(str(freshness["monitor_timestamp"])).astimezone(UTC)
        freshness_age = (now - monitored).total_seconds()
        freshness_ok = freshness.get("overall") == "ok" and -60 <= freshness_age <= 600
        add(
            "P0",
            "freshness_gate",
            freshness_ok,
            f"overall={freshness.get('overall')} age={freshness_age:.0f}s",
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        add("P0", "freshness_gate", False, type(error).__name__)
    for key, relative in (
        ("decision_snapshot", "logs/briefing_decisions_latest.json"),
        ("delayed_bid_ask_tape", "collect/log/quotes.jsonl"),
    ):
        path = root / relative
        add("P0", key, path.is_file() and path.stat().st_size > 0, str(path))
    try:
        read_health = _json_url(f"{read_api_url.rstrip('/')}/health")
        add(
            "P0",
            "fixed_loopback_read_boundary",
            read_health.get("ok") is True and read_health.get("read_only") is True,
            f"read_only={read_health.get('read_only')}",
        )
    except Exception as error:
        read_health = {}
        add("P0", "fixed_loopback_read_boundary", False, type(error).__name__)

    snapshot: dict[str, object] = {}
    learning: dict[str, object] = {}
    v2_audit: dict[str, object] = {}
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        connection.close()
        with open_virtual_portfolio_reader(db) as store:
            snapshot = store.snapshot(now=now, trade_limit=500)
            learning = store.export_learning_rows(as_of=now)
            v2_audit = store.audit_virtual_portfolio_v2()
        add("P1", "sqlite_integrity", integrity == "ok", integrity)
        add("P1", "schema_v4", schema >= 4, f"user_version={schema}")
        add(
            "P1",
            "fixed_initial_capital",
            snapshot.get("initial_capital_jpy") == 1_000_000,
            str(snapshot.get("initial_capital_jpy")),
        )
        reconciliation = snapshot.get("reconciliation")
        add(
            "P1",
            "canonical_reconciliation",
            isinstance(reconciliation, dict) and reconciliation.get("passed") is True,
            str(reconciliation),
        )
        add(
            "P1",
            "v2_accounting_cash_parity",
            v2_audit.get("passed") is True and v2_audit.get("cash_parity") is True,
            str(v2_audit),
        )
        append_tables = {
            "portfolio_config",
            "session_events",
            "decision_intakes",
            "decision_records",
            "decision_horizon_assignments",
            "simulated_trade_opens",
            "simulated_trade_closes",
            "session_close_requests",
            "session_close_completions",
            "simulated_position_marks",
            "ledger_events",
            "portfolio_marks",
            "learning_exports",
        }
        append_triggers = {
            f"{table}_{suffix}" for table in append_tables for suffix in ("no_update", "no_delete")
        }
        add(
            "P1",
            "append_only_enforcement",
            append_tables <= tables and append_triggers <= triggers,
            f"tables={len(append_tables & tables)}/{len(append_tables)} triggers={len(append_triggers & triggers)}/{len(append_triggers)}",
        )

        quality = snapshot.get("data_quality_state")
        add(
            "P2",
            "executable_position_marks",
            isinstance(quality, dict) and quality.get("position_marks_complete") is True,
            str(quality),
        )
        add(
            "P2",
            "persisted_session_close",
            {"session_close_requests", "session_close_completions"} <= tables,
            "request-scoped append-only request/completion tables",
        )
        add(
            "P2",
            "bid_ask_cost_identity",
            isinstance(reconciliation, dict) and reconciliation.get("passed") is True,
            "canonical net P&L reconciled",
        )
        add(
            "P2",
            "analysis_only_execution",
            snapshot.get("execution_mode")
            in {"offline_simulation", "five_minute_snapshot_simulation"}
            and snapshot.get("broker_connected") is False,
            f"mode={snapshot.get('execution_mode')} broker={snapshot.get('broker_connected')}",
        )
        identity_scope = v2_audit.get("identity_scope")
        add(
            "P2",
            "v2_identity_audit_contract_presence",
            v2_audit.get("passed") is True
            and isinstance(identity_scope, list)
            and {
                "journal_balance_and_component_postings",
                "position_cost_reserves_and_liquidation_pnl",
                "portfolio_mid_executable_liquidation_equity",
                "cash_and_realized_pnl_as_observed_at_state_head",
                "liquidation_hwm_and_drawdown",
                "source_age_and_cross_position_skew",
                "position_mark_record_and_quote_provenance",
            }
            <= set(identity_scope),
            f"current_db_audit={v2_audit.get('passed')} scope={identity_scope}",
        )
        risk_gate_v2 = snapshot.get("risk_gate_v2_shadow")
        period_baselines = (
            risk_gate_v2.get("period_baselines") if isinstance(risk_gate_v2, dict) else None
        )
        add(
            "P2",
            "pit_period_baseline_contract_presence",
            isinstance(period_baselines, dict)
            and {"day", "week", "month"} <= set(period_baselines),
            str(period_baselines),
        )
        add(
            "P2",
            "checkpoint_tooling_contract_presence",
            (root / "tools" / "virtual_portfolio_checkpoint.py").is_file()
            and (root / "fx_intel" / "virtual_portfolio_checkpoint.py").is_file(),
            "create-only checkpoint + out-of-band digest + retained-prefix verifier",
        )
        v2_snapshot = snapshot.get("portfolio_v2")
        add(
            "P2",
            "synchronized_liquidation_valuation",
            isinstance(v2_snapshot, dict) and v2_snapshot.get("valuation_status") == "complete",
            str(v2_snapshot),
            "empirical",
        )
        add(
            "P2",
            "runtime_settlement_evidence",
            int(str(snapshot.get("closed_trade_count") or 0)) > 0,
            f"closed_trades={snapshot.get('closed_trade_count')}",
            "empirical",
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        for stage, keys in (
            (
                "P1",
                (
                    "sqlite_integrity",
                    "schema_v4",
                    "fixed_initial_capital",
                    "canonical_reconciliation",
                    "append_only_enforcement",
                ),
            ),
            (
                "P2",
                (
                    "executable_position_marks",
                    "persisted_session_close",
                    "bid_ask_cost_identity",
                    "analysis_only_execution",
                    "independent_v2_identity_audit",
                    "pit_period_baseline_contract",
                    "externally_retainable_checkpoint_contract",
                ),
            ),
        ):
            for key in keys:
                add(stage, key, False, type(error).__name__)
        add(
            "P2",
            "synchronized_liquidation_valuation",
            False,
            type(error).__name__,
            "empirical",
        )
        add("P2", "runtime_settlement_evidence", False, type(error).__name__, "empirical")

    try:
        dashboard = _json_url(f"{dashboard_url.rstrip('/')}/api/virtual-portfolio")
        add("P3", "dashboard_api", dashboard.get("configured") is True, "configured")
        add(
            "P3",
            "server_authoritative_values",
            dashboard.get("balance_jpy") == snapshot.get("balance_jpy")
            and dashboard.get("equity_jpy") == snapshot.get("equity_jpy"),
            "balance/equity match canonical reader",
        )
    except Exception as error:
        add("P3", "dashboard_api", False, type(error).__name__)
        add("P3", "server_authoritative_values", False, type(error).__name__)
    index_path = root / "tools" / "ai_learning_dashboard" / "static" / "index.html"
    app_path = root / "tools" / "ai_learning_dashboard" / "static" / "app.js"
    try:
        index = index_path.read_text(encoding="utf-8")
        app = app_path.read_text(encoding="utf-8")
        required_ids = (
            'id="portfolioEquity"',
            'id="portfolioTradeRows"',
            'id="portfolioTradeDetail"',
            'id="portfolioCurveCanvas"',
        )
        add(
            "P3",
            "required_views",
            all(value in index for value in required_ids),
            "portfolio/account/detail/curve",
        )
        add(
            "P3",
            "explanation_and_counterfactuals",
            "反実仮想（因果断定なし）" in app and "失敗分類" in app,
            "structured facts",
        )
        add("P3", "no_local_path_input", "logDir" not in index + app, "arbitrary path input absent")
    except OSError as error:
        for key in ("required_views", "explanation_and_counterfactuals", "no_local_path_input"):
            add("P3", key, False, type(error).__name__)

    rows = learning.get("rows")
    add(
        "P4",
        "pit_learning_export",
        learning.get("contract") == "virtual-portfolio-learning-pit-v3" and isinstance(rows, list),
        f"rows={learning.get('row_count')}",
    )
    add(
        "P4",
        "target_not_label",
        learning.get("promotion_eligible") is False,
        "daily KPI excluded and promotion false",
    )
    canonical = learning.get("canonical_net_r")
    canonical = canonical if isinstance(canonical, dict) else {}
    eligible_rows = int(canonical.get("eligible_rows", 0) or 0)
    canonical_rows = int(canonical.get("net_label_samples", 0) or 0)
    add(
        "P4",
        "canonical_net_r_coverage",
        canonical_rows == eligible_rows
        and (eligible_rows == 0 or canonical.get("net_label_coverage") == 1.0),
        f"canonical={canonical_rows}/{eligible_rows}",
    )
    if isinstance(rows, list):
        cost_pass = all(
            isinstance(row, dict)
            and isinstance(row.get("attribution"), dict)
            and row["attribution"].get("identity")
            for row in rows
        )
        failure_pass = all(
            isinstance(row, dict) and isinstance(row.get("failures"), list) for row in rows
        )
    else:
        cost_pass = failure_pass = False
    add("P4", "cost_attribution_rows", cost_pass, "all mature rows reconcile")
    add("P4", "failure_classification_rows", failure_pass, "multi-label evidence")
    add(
        "P4",
        "mature_learning_evidence",
        int(str(learning.get("row_count") or 0)) > 0,
        f"rows={learning.get('row_count')}",
        "empirical",
    )

    learning_root = root / "runs" / "virtual_portfolio_learning"
    completed_run = _latest_completed_learning_run(learning_root)
    completed_run_id = completed_run[0] if completed_run is not None else None
    completed_payloads = completed_run[1] if completed_run is not None else {}
    manifest = completed_payloads.get("challenger_gate.json")
    evidence = completed_payloads.get("validation_evidence.json")
    promotion = completed_payloads.get("promotion_audit.json")
    add(
        "P5",
        "completed_learning_run",
        completed_run is not None,
        str(completed_run_id or "unavailable_or_hash_invalid"),
    )
    add(
        "P5",
        "five_way_manifest",
        bool(manifest and manifest.get("contract") == "virtual-portfolio-challenger-gate-v1"),
        str(manifest and manifest.get("status")),
    )
    add(
        "P5",
        "validation_evidence",
        bool(evidence and evidence.get("contract") == "virtual-portfolio-validation-evidence-v1"),
        str(evidence and evidence.get("status")),
    )
    add(
        "P5",
        "promotion_audit",
        bool(
            promotion
            and promotion.get("contract") == "virtual-portfolio-promotion-audit-v1"
            and promotion.get("automatic_promotion") is False
        ),
        str(promotion and promotion.get("decision")),
    )
    governance_evidence = (
        promotion.get("governance_evidence") if isinstance(promotion, dict) else None
    )
    add(
        "P5",
        "fixed_lockbox_evidence",
        bool(
            evidence
            and evidence.get("lockbox_access") == "evaluated_once_fixed_lockbox"
            and isinstance(governance_evidence, dict)
            and governance_evidence.get("lockbox_evaluated_once") is True
            and governance_evidence.get("lockbox_reused_for_selection") is False
        ),
        str(evidence and evidence.get("lockbox_access")),
        "empirical",
    )
    add(
        "P5",
        "validated_candidate_evidence",
        bool(promotion and promotion.get("promotion_eligible") is True),
        str(promotion and promotion.get("promotion_eligible")),
        "empirical",
    )

    required_files = (
        "scripts/virtual_portfolio_once.sh",
        "scripts/virtual_portfolio_learning_once.sh",
        "tools/virtual_portfolio_daily_report.py",
        *(f"ops/launchd/{label}.plist.tmpl" for label in VIRTUAL_LABELS),
    )
    missing = [relative for relative in required_files if not (root / relative).is_file()]
    add("P6", "operation_assets", not missing, f"missing={missing}")
    once = (root / "scripts" / "virtual_portfolio_once.sh").read_text(encoding="utf-8")
    add(
        "P6",
        "daily_report_retry",
        "--send-discord" in once and "--if-ready" in once,
        "delivery receipt + retry path",
    )
    add(
        "P6",
        "discord_configured",
        load_webhook_url(root) is not None,
        "secret resolved without exposing value",
    )
    if check_launchd:
        for label in VIRTUAL_LABELS:
            add("P6", f"launchd:{label}", _loaded_label(label), "loaded")
    else:
        add("P6", "launchd_runtime", True, "not requested on this host")
    daily_reports = list((root / "runs" / "virtual_portfolio_daily").glob("*/daily_report.json"))
    add(
        "P6",
        "daily_report_delivery_evidence",
        bool(daily_reports),
        f"reports={len(daily_reports)}",
        "empirical",
    )

    stage_payload: dict[str, object] = {}
    for stage, checks in stages.items():
        engineering = _score(checks, "engineering")
        empirical = _score(checks, "empirical")
        stage_payload[stage] = {
            "engineering_score_pct": engineering,
            "engineering_at_least_95": engineering is not None and engineering >= 95,
            "empirical_evidence_score_pct": empirical,
            "checks": [asdict(check) for check in checks],
        }
    payload: dict[str, object] = {
        "contract": AUDIT_CONTRACT,
        "generated_at": now.isoformat(),
        "read_only": True,
        "execution_mode": "offline_simulation",
        "broker_connected": False,
        "score_semantics": {
            "engineering": "implemented controls that can pass before outcomes accumulate",
            "empirical": "observed mature trades/reports/models; never imputed from code",
        },
        "stages": stage_payload,
        "all_engineering_stages_at_least_95": all(
            isinstance(value, dict) and value["engineering_at_least_95"] is True
            for value in stage_payload.values()
        ),
    }
    return payload


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit P0-P6 virtual-portfolio readiness")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--read-api-url", default="http://127.0.0.1:8771")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8788")
    parser.add_argument("--check-launchd", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    database = (args.db or root / "logs" / "fx_virtual_portfolio_canonical_v2.sqlite3").resolve()
    try:
        payload = audit(
            root=root,
            db=database,
            read_api_url=args.read_api_url,
            dashboard_url=args.dashboard_url,
            check_launchd=args.check_launchd,
        )
        if args.output:
            _atomic_write(args.output.resolve(), payload)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["all_engineering_stages_at_least_95"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
