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
- **No file is skipped; only inert literals are allowed.** A test that asserts
  an order surface is absent has to spell out the strings it forbids. Skipping
  those files wholesale would make them the one place a real broker order call
  could hide while the suite stayed green. Instead every file is scanned, and
  the modules in ``LITERAL_ALLOWED_FILES`` may carry those tokens *only* fully
  enclosed in quotes; anything executable is reported there like anywhere else.
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

# Guard modules must name the very strings they forbid. Rather than exempting
# those files wholesale — which would leave the prohibited surface a place to
# hide — each *occurrence* is judged: a forbidden token is tolerated only when
# it appears as an inert quoted literal, i.e. the whole token sits inside
# quotes, the way the tuples above name them. An actual call has the
# identifier outside quotes and is still reported, in every file.
INERT_LITERAL_TEMPLATE = r"""["']{token}["']"""

# Files permitted to carry inert literals at all. Membership does NOT skip the
# scan; it only allows the quoted-literal form above. Anything executable in
# these files is still a violation.
LITERAL_ALLOWED_FILES = frozenset(
    {
        "tests/test_no_live_execution_surface.py",
        "tests/test_collect_no_order_path.py",
        # Asserts the OANDA stream URL is pricing-only: "/orders", "/trades",
        # "/positions" must NOT appear in the constructed URL.
        "tests/test_collect_sources.py",
    }
)


def _inert_literal_count(text: str, token: str) -> int:
    """How many times ``token`` appears fully enclosed in quotes."""

    return len(re.findall(INERT_LITERAL_TEMPLATE.format(token=re.escape(token)), text))


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

    for allowed in LITERAL_ALLOWED_FILES:
        assert allowed in scanned, f"literal-allowed file no longer tracked: {allowed}"


def scan_source(label: str, text: str, *, allow_inert_literals: bool = False) -> list[str]:
    """Return every broker-order violation found in one source text.

    ``allow_inert_literals`` tolerates occurrences that are entirely enclosed in
    quotes, which is how the guard modules name what they forbid. Every other
    occurrence is reported even in those files, so an executable call can never
    hide behind the allowance.
    """

    violations: list[str] = []
    for snippet in FORBIDDEN_ACTIVE_SOURCE_SNIPPETS:
        total = text.count(snippet)
        if not total:
            continue
        inert = _inert_literal_count(text, snippet) if allow_inert_literals else 0
        if total > inert:
            violations.append(f"{label}: {snippet}")

    for match in FORBIDDEN_ENDPOINT_PATTERN.finditer(text):
        endpoint = match.group(0)
        # A bare one-segment literal is inert; a multi-segment broker URL is not.
        if allow_inert_literals and re.fullmatch(r"""["']/\w+["']""", endpoint):
            continue
        violations.append(f"{label}: broker order endpoint {endpoint}")
    return violations


def test_active_source_has_no_broker_order_surface() -> None:
    violations: list[str] = []

    for path in tracked_python_files():
        name = path.as_posix()
        violations.extend(
            scan_source(
                name,
                (ROOT / path).read_text(encoding="utf-8"),
                allow_inert_literals=name in LITERAL_ALLOWED_FILES,
            )
        )

    assert not violations, "broker execution surface detected:\n" + "\n".join(violations)


def test_scanner_detects_a_planted_order_client() -> None:
    """The detector must fail on the code it exists to catch.

    Without this, a scan that silently stopped matching would still report a
    green suite: an all-clear from a broken detector is indistinguishable from
    an all-clear from a clean repository.
    """

    # Assembled from fragments so this module never itself contains a non-inert
    # broker call: the scan would (correctly) flag its own fixture otherwise.
    order_call = "place" + "Order(" + "Market" + "Order('BUY', 1000))"
    broker_url = "https://api.broker.com/v3/accounts/1/" + "orders" + "/"
    planted = (
        "import os\n"
        "def send():\n"
        f"    if os.environ.get({'ALLOW' + '_LIVE'!r}) == '1':\n"
        f"        ib.{order_call}\n"
        f"        session.post({broker_url!r})\n"
    )
    found = scan_source("scripts/planted_probe.py", planted)

    assert any("placeOrder(" in item for item in found)
    assert any("MarketOrder(" in item for item in found)
    assert any("ALLOW_LIVE" in item for item in found)
    assert any("broker order endpoint" in item for item in found)


def test_literal_allowance_does_not_hide_an_executable_call() -> None:
    """The guard files get no safe harbour for real execution code.

    ``allow_inert_literals`` exists so a guard can name what it forbids. If it
    were a whole-file skip, those modules would become the one place a real
    broker order call could sit unnoticed — the suite would stay green while
    the boundary was already broken.
    """

    # Naming the tokens as quoted literals is what a guard legitimately does.
    naming_only = 'FORBIDDEN = ("place' + 'Order(", "ALLOW' + '_LIVE", "/orders")\n'
    assert scan_source("tests/guard.py", naming_only, allow_inert_literals=True) == []

    # The same file must not get away with an executable call.
    order_call = "place" + "Order(" + "Market" + "Order(1))"
    executable = naming_only + f"def send():\n    ib.{order_call}\n"
    found = scan_source("tests/guard.py", executable, allow_inert_literals=True)
    assert any("placeOrder(" in item for item in found)
    assert any("MarketOrder(" in item for item in found)

    broker_url = "https://api.broker.com/v3/1/" + "orders" + "/"
    live_url = naming_only + f"session.post({broker_url!r})\n"
    assert any(
        "broker order endpoint" in item
        for item in scan_source("tests/guard.py", live_url, allow_inert_literals=True)
    )


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
