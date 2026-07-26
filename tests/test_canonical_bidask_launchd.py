from __future__ import annotations

import json
from pathlib import Path
import plistlib

ROOT = Path(__file__).resolve().parents[1]


def _rendered_plist(label: str) -> dict:
    template = ROOT / "ops" / "launchd" / f"{label}.plist.tmpl"
    rendered = (
        template.read_text(encoding="utf-8")
        .replace("__ROOT__", "/srv/fx-codex")
        .replace("__PYTHON__", "/srv/fx-codex/.venv/bin/python")
        .replace("__HOME__", "/Users/fx")
    )
    return plistlib.loads(rendered.encode("utf-8"))


def test_quote_collector_writes_under_the_selected_runtime_root() -> None:
    payload = _rendered_plist("com.fx-codex.quote-collector")
    arguments = payload["ProgramArguments"]

    assert arguments[-2:] == ["--output-root", "/srv/fx-codex/collect"]
    assert payload["WorkingDirectory"] == "/srv/fx-codex"
    assert payload["StandardOutPath"].startswith("/srv/fx-codex/collect/")
    assert payload["StandardErrorPath"].startswith("/srv/fx-codex/collect/")


def test_canonical_services_create_private_runtime_files() -> None:
    for label in (
        "com.fx-codex.quote-collector",
        "com.fx-codex.bidask-materializer",
        "com.fx-codex.bidask-health",
    ):
        assert _rendered_plist(label)["Umask"] == 0o77


def test_runtime_collection_root_cannot_dirty_the_approved_checkout() -> None:
    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "/collect/" in patterns
    assert "collect/" not in patterns


def test_materializer_reads_only_committed_ingest_and_uses_a_separate_shadow_label() -> None:
    payload = _rendered_plist("com.fx-codex.bidask-materializer")
    arguments = payload["ProgramArguments"]

    assert payload["Label"] == "com.fx-codex.bidask-materializer"
    assert "com.fx-codex.snapshot" not in payload["Label"]
    assert "/srv/fx-codex/tools/materialize_bid_ask_prices.py" in arguments
    assert arguments[arguments.index("--ingest-log-dir") + 1] == "/srv/fx-codex/collect/log"
    assert (
        arguments[arguments.index("--output") + 1]
        == "/srv/fx-codex/logs/briefing_tf_bidask_prices.sqlite3"
    )
    assert "--state" not in arguments
    assert payload["StartInterval"] == 60
    assert "--timeout-seconds" not in arguments


def test_canonical_health_uses_isolated_state_and_report_files() -> None:
    payload = _rendered_plist("com.fx-codex.bidask-health")
    arguments = payload["ProgramArguments"]

    assert payload["Label"] == "com.fx-codex.bidask-health"
    assert (
        arguments[arguments.index("--config") + 1] == "ops/freshness_targets_canonical_bidask.json"
    )
    assert arguments[arguments.index("--state") + 1] == "logs/canonical_bidask_freshness_state.json"
    assert (
        arguments[arguments.index("--report") + 1] == "logs/canonical_bidask_freshness_report.json"
    )
    assert "--timeout-seconds" not in arguments


def test_canonical_freshness_requires_hash_provenance_and_full_universe() -> None:
    payload = json.loads(
        (ROOT / "ops" / "freshness_targets_canonical_bidask.json").read_text(encoding="utf-8")
    )
    assert payload["market_hours_only"] is True
    targets = {target["name"]: target for target in payload["targets"]}
    prices = targets["canonical_bidask_prices"]

    assert prices["required_schema_version"] == 3
    assert prices["verify_content_hash"] is True
    assert prices["kind"] == "canonical_bidask_sqlite"
    assert prices["required_symbols"] == ["USDJPY", "EURUSD", "GBPUSD", "AUDUSD"]
    assert prices["required_timeframes"] == ["15m", "1h", "4h", "1d"]


def test_installer_requires_clean_approved_sha_and_checks_legacy_checkouts() -> None:
    script = (ROOT / "scripts" / "canonical_bidask_shadow_launchd.sh").read_text(encoding="utf-8")

    assert "FX_CODEX_APPROVED_SHA" in script
    assert "status --porcelain=v1 --untracked-files=all" in script
    assert '"$HOME/Desktop/fx-codex" "$HOME/fx-codex"' in script
    assert "'trader/**'" in script
    assert 'quote_collector_launchd.sh" dry-run' in script
    assert "既存label/plistを上書きしません" in script


def test_direct_collector_installer_cannot_bypass_approved_sha_gate() -> None:
    script = (ROOT / "scripts" / "quote_collector_launchd.sh").read_text(encoding="utf-8")

    assert "FX_CODEX_APPROVED_SHA" in script
    assert '"${#approved_sha}" == 40' in script
    assert "status --porcelain=v1 --untracked-files=all" in script
