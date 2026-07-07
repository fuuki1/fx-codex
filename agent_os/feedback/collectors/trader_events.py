"""Feedback collector for exported trader event JSONL."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_os.feedback.schemas import FeedbackEvent
from agent_os.sessions.schemas import AgentOSValidationError, utc_now_iso

HIGH_RISK_TOKENS = (
    "dead_letter",
    "risk_block",
    "risk_reject",
    "order_reject",
    "daily_loss",
    "circuit_breaker",
    "exposure",
)
GAP_TOKENS = ("missing", "unavailable", "timeout", "stale")
REJECT_TOKENS = ("reject", "blocked", "denied")


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def collect_trader_events_jsonl(
    *,
    session_id: str,
    jsonl_text: str,
    evidence_path: str | Path,
    feedback_id_prefix: str = "fb",
) -> list[FeedbackEvent]:
    """Collect actionable feedback from exported trader events JSONL."""

    evidence = str(evidence_path)
    events: list[FeedbackEvent] = []
    for line_no, line in enumerate(jsonl_text.splitlines(), start=1):
        if not line.strip():
            continue
        record = _parse_jsonl_line(line, line_no)
        kind = str(record.get("kind", "")).strip()
        if not kind:
            raise AgentOSValidationError(f"trader event line {line_no} missing kind")
        payload = _payload(record.get("payload", {}), line_no)
        classification = _classify_kind(kind)
        if classification is None:
            continue
        category, severity, target, title = classification
        reason = _reason(payload)
        summary = f"{kind}: {reason}" if reason else kind
        events.append(
            FeedbackEvent(
                feedback_id=_stable_id(
                    feedback_id_prefix,
                    session_id,
                    "trader_events",
                    str(line_no),
                    kind,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    evidence,
                ),
                source="trader_events",
                session_id=session_id,
                severity=severity,
                category=category,
                summary=summary[:500],
                evidence=[evidence],
                recommended_change={"target": target, "title": title},
                metadata={
                    "line": line_no,
                    "kind": kind,
                    "payload_keys": sorted(payload.keys()),
                    "ts": str(record.get("ts", "")),
                },
                created_at=utc_now_iso(),
            )
        )
    return events


def _parse_jsonl_line(line: str, line_no: int) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise AgentOSValidationError(
            f"Invalid trader events JSONL at line {line_no}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise AgentOSValidationError(f"trader event line {line_no} must be a JSON object")
    return value


def _payload(value: Any, line_no: int) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"message": value}
        if isinstance(parsed, dict):
            return dict(parsed)
    raise AgentOSValidationError(f"trader event payload at line {line_no} must be object-like")


def _classify_kind(kind: str) -> tuple[str, str, str, str] | None:
    lowered = kind.lower()
    if any(token in lowered for token in HIGH_RISK_TOKENS):
        return (
            "ops_event",
            "high",
            "runbook",
            "Document trader high-risk event response",
        )
    if any(token in lowered for token in GAP_TOKENS):
        return (
            "runbook_gap",
            "medium",
            "runbook",
            "Document trader missing dependency response",
        )
    if any(token in lowered for token in REJECT_TOKENS):
        return (
            "ops_event",
            "medium",
            "runbook",
            "Document trader rejection response",
        )
    return None


def _reason(payload: dict[str, Any]) -> str:
    for key in ("reason", "error", "message", "detail", "status"):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    if payload:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:200]
    return ""
