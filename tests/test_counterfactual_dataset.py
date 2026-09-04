from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fx_intel.counterfactual_dataset import (
    DATASET_CONTRACT,
    build_dataset_manifest,
    build_label_schema,
    load_counterfactual_run,
    write_counterfactual_run,
)

AS_OF = datetime(2026, 9, 4, 2, 0, tzinfo=UTC)


def _rows() -> list[dict[str, object]]:
    return [
        {
            "decision_id": "d-2",
            "t": "2026-09-01T01:00:00+00:00",
            "symbol": "USDJPY",
            "timeframe": "15m",
            "heuristic_direction": "short",
            "long_net_r": -0.11,
            "short_net_r": 0.04,
            "abstain_net_r": 0.0,
            "long_cost_r": 0.0424,
            "short_cost_r": 0.0424,
        },
        {
            "decision_id": "d-1",
            "t": "2026-09-01T00:00:00+00:00",
            "symbol": "EURUSD",
            "timeframe": "1h",
            "heuristic_direction": "long",
            "long_net_r": 0.02,
            "short_net_r": -0.09,
            "abstain_net_r": 0.0,
            "long_cost_r": 0.0424,
            "short_cost_r": 0.0424,
        },
    ]


def _manifest(rows, tmp_path: Path, **kw) -> dict[str, object]:
    return build_dataset_manifest(
        rows,
        unavailable_reasons=kw.get("reasons", {"no_executable_tick_in_entry_window": 489}),
        considered_count=kw.get("considered", len(rows) + 489),
        quote_policy_sha256="a" * 64,
        cost_policy_sha256="b" * 64,
        tape_source={"path": "/tmp/quotes.jsonl", "prefix_sha256": "c" * 64},
        as_of=AS_OF,
        repo_root=tmp_path,
    )


def test_run_is_written_read_only_and_reloads(tmp_path) -> None:
    rows = _rows()
    run = write_counterfactual_run(tmp_path / "runs", rows=rows, manifest=_manifest(rows, tmp_path))
    for name in ("dataset.jsonl", "manifest.json", "label_schema.json", "completion.json"):
        target = run / name
        assert target.is_file()
        assert oct(target.stat().st_mode)[-3:] == "444", f"{name} must be read-only"
    reloaded = load_counterfactual_run(run)
    assert reloaded["manifest"]["contract"] == DATASET_CONTRACT
    assert len(reloaded["rows"]) == 2


def test_rows_are_stored_in_a_deterministic_order(tmp_path) -> None:
    """The dataset hash must not depend on the order rows happened to arrive."""

    forward = _rows()
    backward = list(reversed(_rows()))
    a = build_dataset_manifest(
        forward,
        unavailable_reasons={},
        considered_count=2,
        quote_policy_sha256="a" * 64,
        cost_policy_sha256="b" * 64,
        tape_source={},
        as_of=AS_OF,
        repo_root=tmp_path,
    )
    b = build_dataset_manifest(
        backward,
        unavailable_reasons={},
        considered_count=2,
        quote_policy_sha256="a" * 64,
        cost_policy_sha256="b" * 64,
        tape_source={},
        as_of=AS_OF,
        repo_root=tmp_path,
    )
    assert a["dataset_sha256"] == b["dataset_sha256"]


def test_publishing_the_same_run_twice_is_refused(tmp_path) -> None:
    rows = _rows()
    manifest = _manifest(rows, tmp_path)
    write_counterfactual_run(tmp_path / "runs", rows=rows, manifest=manifest)
    with pytest.raises(FileExistsError):
        write_counterfactual_run(tmp_path / "runs", rows=rows, manifest=manifest)


def test_a_tampered_dataset_fails_to_reload(tmp_path) -> None:
    """Immutability is enforced by hash, not only by file mode."""

    rows = _rows()
    run = write_counterfactual_run(tmp_path / "runs", rows=rows, manifest=_manifest(rows, tmp_path))
    dataset = run / "dataset.jsonl"
    os.chmod(dataset, 0o644)
    dataset.write_text(dataset.read_text().replace("-0.11", "9.99"))
    with pytest.raises(ValueError):
        load_counterfactual_run(run)


def test_manifest_records_what_was_excluded(tmp_path) -> None:
    """Coverage has to be legible: a gap is reported, not silently dropped."""

    rows = _rows()
    manifest = _manifest(rows, tmp_path)
    assert manifest["row_count"] == 2
    assert manifest["considered_count"] == 491
    assert manifest["excluded_count"] == 489
    assert manifest["coverage"] == pytest.approx(2 / 491)
    assert manifest["counterfactual_unavailable_reasons"] == {
        "no_executable_tick_in_entry_window": 489
    }


def test_provenance_marks_the_run_as_research_only(tmp_path) -> None:
    rows = _rows()
    manifest = _manifest(rows, tmp_path)
    assert manifest["measured"] is False
    assert manifest["promotion_eligible"] is False
    assert manifest["broker_connected"] is False
    assert manifest["tradable"] is False
    assert manifest["execution_target"] == "shadow_decision_only"
    assert manifest["replay_quote_policy_sha256"] == "a" * 64
    assert manifest["replay_cost_policy_sha256"] == "b" * 64


def test_label_schema_states_gross_is_derivable_and_labels_are_not_measured() -> None:
    schema = build_label_schema()
    assert schema["measured"] is False
    assert "gross_r = net_r + cost_r" in str(schema["gross_is_derivable"])
    assert set(schema["actions"]) == {"long", "short", "abstain"}
    assert "not be presented as executed fills" in str(schema["not_measured"])


def test_completion_hashes_every_published_file(tmp_path) -> None:
    rows = _rows()
    run = write_counterfactual_run(tmp_path / "runs", rows=rows, manifest=_manifest(rows, tmp_path))
    completion = json.loads((run / "completion.json").read_text())
    assert set(completion["files"]) == {"dataset.jsonl", "manifest.json", "label_schema.json"}
    assert completion["row_count"] == 2


def _git_repo(root: Path) -> None:
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=False)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "seed")


def test_clean_tree_is_reproducible_from_its_commit(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    manifest = _manifest(_rows(), repo)
    assert manifest["dirty_worktree"] is False
    assert manifest["reproducible_from_commit_alone"] is True
    assert manifest["working_tree_diff_sha256"] is None


def test_dirty_tree_pins_the_difference_it_was_built_from(tmp_path) -> None:
    """A commit alone cannot rebuild a dirty tree, so the diff is hashed too."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / "seed.txt").write_text("seed\nmodified\n")
    dirty = _manifest(_rows(), repo)
    assert dirty["dirty_worktree"] is True
    assert dirty["reproducible_from_commit_alone"] is False
    assert isinstance(dirty["working_tree_diff_sha256"], str)
    assert len(str(dirty["working_tree_diff_sha256"])) == 64

    # A different working tree, same commit, must produce a different digest.
    (repo / "seed.txt").write_text("seed\nmodified differently\n")
    other = _manifest(_rows(), repo)
    assert other["git_commit"] == dirty["git_commit"]
    assert other["working_tree_diff_sha256"] != dirty["working_tree_diff_sha256"]


def test_untracked_files_change_the_working_tree_digest(tmp_path) -> None:
    """Untracked files never appear in `git diff`, so they are hashed separately."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / "seed.txt").write_text("seed\nmodified\n")
    before = _manifest(_rows(), repo)
    (repo / "brand_new_module.py").write_text("# produced this dataset\n")
    after = _manifest(_rows(), repo)
    assert after["working_tree_diff_sha256"] != before["working_tree_diff_sha256"]
