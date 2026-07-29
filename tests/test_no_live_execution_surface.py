"""Permanent repository boundary: no broker order execution surface.

Scope note: the source scan below enumerates **every tracked ``*.py`` file**
via ``git ls-files`` rather than a hand-maintained directory list. An earlier
version scanned only the repository root plus four package directories, so a
file added under ``scripts/``, ``examples/``, or any new package escaped the
check entirely. Deriving the file list from git means a newly added package is
covered the moment it is tracked, with no test edit required.

Two deliberate design choices:

- **Broker-order network calls, not simulation vocabulary.** ``fx_backtester``
  legitimately owns ``_close_position`` / ``_schedule_close_order``: these are
  in-memory backtest bookkeeping with no network access. Forbidding the noun
  "order" outright would flag the simulator and pressure future authors to
  rename honest code, so the patterns below target broker SDK constructors,
  live-trading switches, and broker REST order endpoints instead.
- **The guard files exempt themselves.** A test that asserts an order surface
  is absent has to spell out the very strings it forbids, so a repository-wide
  scan would otherwise always fail on its own source. The exemption is an
  explicit allowlist in ``SCAN_EXEMPT_FILES`` — currently three files, each
  justified there. Every other file, tests included, is scanned normally.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

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

# Broker REST order endpoints. Matching the quoted path keeps ordinary words
# ("trades", "positions") in prose and variable names out of the result.
FORBIDDEN_ENDPOINT_PATTERN = re.compile(
    r"""["'][^"']*/(orders|trades|positions|transactions)(/|["'])"""
)

# The only files allowed to contain the strings above: tests that assert the
# absence of an order surface must name the very strings they forbid.
# ``test_collect_sources.py`` qualifies because it asserts the OANDA stream URL
# is pricing-only ("/orders", "/trades", "/positions" must NOT appear in it) —
# same intent as this module, expressed against a constructed URL.
SCAN_EXEMPT_FILES = frozenset(
    {
        "tests/test_no_live_execution_surface.py",
        "tests/test_collect_no_order_path.py",
        "tests/test_collect_sources.py",
    }
)


def tracked_python_files() -> list[Path]:
    """Every tracked ``*.py`` path, so new packages are covered automatically."""

    result = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(name) for name in result.stdout.split("\0") if name]


def test_broker_execution_paths_are_absent() -> None:
    existing = [path for path in FORBIDDEN_PATHS if (ROOT / path).exists()]
    assert not existing, f"broker execution paths restored: {existing}"


def test_tracked_source_scan_covers_every_package() -> None:
    """The scan must reach directories the old hand-maintained list omitted."""

    scanned = {path.as_posix() for path in tracked_python_files()}
    covered_dirs = {path.split("/", 1)[0] if "/" in path else "<root>" for path in scanned}

    # Regression guard for the exact gap this test was written to close.
    for directory in ("scripts", "examples", "tests"):
        assert directory in covered_dirs, f"{directory}/ escaped the source scan"
    assert "<root>" in covered_dirs, "root-level modules escaped the source scan"

    for exempt in SCAN_EXEMPT_FILES:
        assert exempt in scanned, f"exempt file no longer tracked: {exempt}"


def scan_source(label: str, text: str) -> list[str]:
    """Return every broker-order violation found in one source text."""

    violations = [
        f"{label}: {snippet}" for snippet in FORBIDDEN_ACTIVE_SOURCE_SNIPPETS if snippet in text
    ]
    endpoint = FORBIDDEN_ENDPOINT_PATTERN.search(text)
    if endpoint is not None:
        violations.append(f"{label}: broker order endpoint {endpoint.group(0)}")
    return violations


def test_active_source_has_no_broker_order_surface() -> None:
    violations: list[str] = []

    for path in tracked_python_files():
        if path.as_posix() in SCAN_EXEMPT_FILES:
            continue
        violations.extend(scan_source(path.as_posix(), (ROOT / path).read_text(encoding="utf-8")))

    assert not violations, "broker execution surface detected:\n" + "\n".join(violations)


def test_scanner_detects_a_planted_order_client() -> None:
    """The detector must fail on the code it exists to catch.

    Without this, a scan that silently stopped matching would still report a
    green suite: an all-clear from a broken detector is indistinguishable from
    an all-clear from a clean repository.
    """

    planted = (
        "import os\n"
        "def send():\n"
        "    if os.environ.get('ALLOW_LIVE') == '1':\n"
        "        ib.placeOrder(MarketOrder('BUY', 1000))\n"
        "        session.post('https://api.broker.com/v3/accounts/1/orders/', json={})\n"
    )
    found = scan_source("scripts/planted_probe.py", planted)

    assert any("placeOrder(" in item for item in found)
    assert any("MarketOrder(" in item for item in found)
    assert any("ALLOW_LIVE" in item for item in found)
    assert any("broker order endpoint" in item for item in found)


def test_scanner_allows_backtest_position_bookkeeping() -> None:
    """Simulation vocabulary must stay legal.

    ``fx_backtester`` closes positions in memory with no network access. If the
    scan flagged the word "order", honest simulator code would have to be
    renamed to keep the suite green — pressure that makes the boundary weaker,
    not stronger.
    """

    simulation = (
        "def _close_position(self, position, timestamp):\n"
        "    return self._schedule_close_order(position, timestamp)\n"
    )
    assert scan_source("fx_backtester/engine.py", simulation) == []


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
