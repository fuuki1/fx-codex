"""Safety isolation: the collector must be incapable of trading.

Verifies (1) importing the collect package never imports an order/executor
module, (2) no public attribute looks like an order method, (3) the source
code contains no order/trade/position endpoint path and no live-trading
switch, (4) tracked files leak no credential-shaped strings.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import re
import subprocess
import sys

COLLECT_DIR = Path(__file__).resolve().parents[1] / "data_platform" / "collect"
COLLECT_MODULES = [
    "data_platform.collect",
    "data_platform.collect.contract",
    "data_platform.collect.raw_first",
    "data_platform.collect.reconnect",
    "data_platform.collect.oanda",
    "data_platform.collect.dukascopy",
    "data_platform.collect.fred_macro",
    "data_platform.collect.divergence",
]

FORBIDDEN_ENDPOINTS = ("/orders", "/trades", "/positions", "/transactions")
FORBIDDEN_SWITCHES = ("ALLOW_LIVE", "place_order", "submit_order", "cancel_order")
ORDER_METHOD_PATTERN = re.compile(
    r"(place|submit|cancel|modify|close)_?(order|trade|position)", re.IGNORECASE
)


def test_collect_never_imports_order_or_executor_modules() -> None:
    for name in COLLECT_MODULES:
        importlib.import_module(name)
    loaded = sorted(sys.modules)
    offenders = [
        module
        for module in loaded
        if module.split(".")[0] == "trader" or "executor" in module.split(".")[-1]
    ]
    assert offenders == [], f"collector imported order-path modules: {offenders}"


def test_no_order_like_public_methods() -> None:
    for name in COLLECT_MODULES:
        module = importlib.import_module(name)
        for attr_name in dir(module):
            assert not ORDER_METHOD_PATTERN.search(
                attr_name
            ), f"{name}.{attr_name} looks like an order method"
            attr = getattr(module, attr_name)
            if isinstance(attr, type):
                for member in dir(attr):
                    assert not ORDER_METHOD_PATTERN.search(
                        member
                    ), f"{name}.{attr_name}.{member} looks like an order method"


def test_source_contains_no_order_endpoint_or_live_switch() -> None:
    for source in sorted(COLLECT_DIR.glob("*.py")):
        text = source.read_text()
        for endpoint in FORBIDDEN_ENDPOINTS:
            assert endpoint not in text, f"{source.name} references {endpoint}"
        for switch in FORBIDDEN_SWITCHES:
            assert switch not in text, f"{source.name} references {switch}"


SECRET_PATTERNS = [
    # OANDA personal access tokens look like 32hex-32hex
    re.compile(r"[0-9a-f]{32}-[0-9a-f]{32}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----"),
    re.compile(r"(api|access)[-_]?(key|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{24,}"),
]
SCAN_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".json", ".md", ".plist", ".toml"}


def test_tracked_files_leak_no_credentials() -> None:
    """Secrets scan over git-tracked collector/ops/config files."""

    repo_root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    leaks: list[str] = []
    for rel in tracked:
        path = repo_root / rel
        if path.suffix not in SCAN_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                snippet = match.group(0)
                # HistData's public form token is a page-scoped download token,
                # not a credential — but we do not whitelist it: the pattern
                # below requires the 32hex-32hex OANDA shape or key=value.
                leaks.append(f"{rel}: {snippet[:24]}…")
    assert leaks == [], f"credential-shaped strings found in tracked files: {leaks}"


def test_env_files_are_not_tracked() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    offenders = [
        rel
        for rel in tracked
        if Path(rel).name in (".env", ".env.local") and not rel.startswith("trader/")
    ]
    # trader/.env.example is a template (no values); real .env files must not be tracked.
    assert offenders == [], f".env files must never be committed: {offenders}"
