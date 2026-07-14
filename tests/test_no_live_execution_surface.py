"""Permanent repository boundary: no broker order execution surface."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PATHS = (
    "trader",
    "executor.py",
    "auto_optimize.py",
    "promote_params.py",
    "params_gate.py",
    "strategy_params.json",
)

FORBIDDEN_ACTIVE_SOURCE_SNIPPETS = (
    "placeOrder(",
    "MarketOrder(",
    "LimitOrder(",
    "StopOrder(",
    "ALLOW_LIVE",
    "IB_PORT_LIVE",
)

ACTIVE_SOURCE_DIRS = (
    "fx_backtester",
    "fx_intel",
    "data_platform",
    "tools",
)


def test_broker_execution_paths_are_absent() -> None:
    existing = [path for path in FORBIDDEN_PATHS if (ROOT / path).exists()]
    assert not existing, f"broker execution paths restored: {existing}"


def test_active_source_has_no_broker_order_surface() -> None:
    violations: list[str] = []

    source_files: list[Path] = list(ROOT.glob("*.py"))
    for directory in ACTIVE_SOURCE_DIRS:
        source_files.extend((ROOT / directory).rglob("*.py"))

    for path in source_files:
        text = path.read_text(encoding="utf-8")
        for snippet in FORBIDDEN_ACTIVE_SOURCE_SNIPPETS:
            if snippet in text:
                violations.append(f"{path.relative_to(ROOT)}: {snippet}")

    assert not violations, "broker execution surface detected:\n" + "\n".join(violations)


def test_ci_has_no_trader_jobs() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "trader-test:" not in workflow
    assert "trader-build-image:" not in workflow
    assert "working-directory: trader" not in workflow


def test_agent_rules_make_analysis_only_permanent() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "permanently analysis-only" in agents
    assert "no automated-trading start phase" in agents
    assert "自動売買開始フェーズは存在しません" in claude
    assert "paper/live broker executionへの昇格は存在しない" in claude
