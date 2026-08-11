"""Regression test for the canonical decision JSONL batch writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fx_intel import decision_log


def test_serialization_failure_does_not_partially_append(tmp_path: Path) -> None:
    class ExplodingMapping(dict[str, object]):
        def items(self):  # type: ignore[no-untyped-def, override]
            raise RuntimeError("serialization failed")

    source = tmp_path / "decisions.jsonl"
    original = f"{json.dumps({'decision_id': 'existing'})}\n".encode()
    source.write_bytes(original)

    with pytest.raises(RuntimeError, match="serialization failed"):
        decision_log.append_decision_events(
            source,
            [{"decision_id": "valid"}, ExplodingMapping()],
        )

    assert source.read_bytes() == original
