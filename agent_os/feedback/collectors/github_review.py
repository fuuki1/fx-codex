"""Feedback collector for exported GitHub review comments."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_os.feedback.schemas import FeedbackEvent
from agent_os.sessions.schemas import utc_now_iso

BODY_KEYS = ("body", "comment", "text", "message")
CONTAINER_KEYS = ("comments", "reviews", "reviewComments", "review_threads", "nodes")


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def collect_github_review(
    *,
    session_id: str,
    review_text: str,
    evidence_path: str | Path,
    feedback_id_prefix: str = "fb",
) -> list[FeedbackEvent]:
    """Collect feedback from saved GitHub review JSON or plain text.

    The collector expects exported review data, not live API access. Supported
    JSON shapes include lists of comment objects, objects with `comments`,
    `reviews`, or `nodes`, and plain text fallback.
    """

    evidence = str(evidence_path)
    items = _review_items(review_text)
    events: list[FeedbackEvent] = []
    for index, item in enumerate(items):
        body = item["body"].strip()
        if not body:
            continue
        path = item.get("path", "")
        line = item.get("line", "")
        author = item.get("author", "")
        state = item.get("state", "")
        severity = _review_severity(body, state)
        category = _review_category(body, state)
        target, title = _recommended_change(body, category)
        location = f"{path}:{line}" if path and line else path
        summary = body.splitlines()[0].strip()
        if location:
            summary = f"{location}: {summary}"
        events.append(
            FeedbackEvent(
                feedback_id=_stable_id(
                    feedback_id_prefix,
                    session_id,
                    "github_review",
                    str(index),
                    body,
                    path,
                    str(line),
                    evidence,
                ),
                source="github_review",
                session_id=session_id,
                severity=severity,
                category=category,
                summary=summary[:500],
                evidence=[evidence],
                recommended_change={"target": target, "title": title},
                metadata={
                    "path": path,
                    "line": line,
                    "author": author,
                    "state": state,
                },
                created_at=utc_now_iso(),
            )
        )
    return events


def _review_items(review_text: str) -> list[dict[str, str]]:
    stripped = review_text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return [{"body": stripped, "path": "", "line": "", "author": "", "state": ""}]
    return _items_from_payload(payload)


def _items_from_payload(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, list):
        items: list[dict[str, str]] = []
        for item in payload:
            items.extend(_items_from_payload(item))
        return items
    if not isinstance(payload, dict):
        return []

    nested: list[dict[str, str]] = []
    for key in CONTAINER_KEYS:
        if key in payload:
            nested.extend(_items_from_payload(payload[key]))
    if nested:
        return nested

    body = _first_text(payload, BODY_KEYS)
    if not body:
        return []
    return [
        {
            "body": body,
            "path": _first_text(payload, ("path", "file", "filename")),
            "line": _first_text(payload, ("line", "originalLine", "position")),
            "author": _author(payload.get("author") or payload.get("user")),
            "state": _first_text(payload, ("state", "reviewState")),
        }
    ]


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _author(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _first_text(value, ("login", "name"))
    return ""


def _review_severity(body: str, state: str) -> str:
    lowered = f"{body} {state}".lower()
    if any(token in lowered for token in ("secret", "credential", "security", "vulnerability")):
        return "critical"
    if any(token in lowered for token in ("blocking", "must", "regression", "data loss", "bug")):
        return "high"
    if any(token in lowered for token in ("nit", "typo", "optional")):
        return "low"
    if state.lower() == "changes_requested":
        return "high"
    return "medium"


def _review_category(body: str, state: str) -> str:
    lowered = f"{body} {state}".lower()
    if any(token in lowered for token in ("secret", "credential", "security", "vulnerability")):
        return "unsafe_tool_use"
    if state.lower() == "changes_requested" or any(
        token in lowered for token in ("blocking", "must", "regression", "bug")
    ):
        return "review_blocker"
    return "review_suggestion"


def _recommended_change(body: str, category: str) -> tuple[str, str]:
    lowered = body.lower()
    if category == "unsafe_tool_use":
        return "tool_policy", "Review security-sensitive review feedback"
    if any(token in lowered for token in ("always", "checklist", "workflow", "repeat")):
        return "skill", "Capture repeated review workflow as a Skill candidate"
    if category == "review_blocker":
        return "issue", "Resolve blocking GitHub review feedback"
    return "issue", "Review GitHub review suggestion"
