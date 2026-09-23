"""Bounded producer status and source-watermark observability.

``generated_at`` is observation time only. Freshness comes from logical source
watermarks, dependencies, market state, and producer health.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Literal

from fx_intel.market import is_market_open

CONTRACT = "fx-runtime-status-v2"
JOB_CONTRACT = "fx-producer-job-status-v1"
MAX_STATUS_BYTES = 65536
MAX_TAIL_BYTES = 1024 * 1024
JobState = Literal["queued", "running", "blocked", "failed", "completed"]
JOB_STATES = frozenset({"queued", "running", "blocked", "failed", "completed"})
MAX_CLOCK_SKEW_SECONDS = 60.0
SOURCE_MAX_AGE_SECONDS: dict[str, float] = {
    "tf_prices": 900,
    "briefing_decisions": 1800,
    "dukascopy_quotes": 10800,
    "quote_index": 10800,
    "freshness_monitor": 900,
    "virtual_portfolio": 900,
    "dashboard_state": 2100,
}
MARKET_SENSITIVE_SOURCES = frozenset({"tf_prices", "dukascopy_quotes", "quote_index"})
DASHBOARD_INPUT_SOURCES = frozenset(
    {"tf_prices", "briefing_decisions", "dukascopy_quotes", "quote_index", "virtual_portfolio"}
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _aware_utc(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def atomic_json(path: Path, payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_STATUS_BYTES:
        raise ValueError("runtime status exceeds bounded document size")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_small_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_STATUS_BYTES + 1)
    if len(raw) > MAX_STATUS_BYTES:
        raise ValueError("runtime status exceeds bounded document size")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("runtime status must be an object")
    return payload


def publish_job_status(
    path: Path,
    *,
    job_id: str,
    state: JobState,
    input_watermark: object = None,
    build_started_at: str | None = None,
    build_completed_at: str | None = None,
    published_at: str | None = None,
    stage: str | None = None,
    progress: object = None,
    blocked_reason: str | None = None,
    failure_reason: str | None = None,
    observed_at: datetime | None = None,
    previous_completed: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Atomically publish one strict, small lifecycle document."""
    if state not in JOB_STATES:
        raise ValueError(f"unsupported job state: {state}")
    moment = observed_at or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    moment = moment.astimezone(UTC)
    if state == "completed" and (build_completed_at is None or published_at is None):
        raise ValueError("completed status requires build_completed_at and published_at")
    if state == "blocked" and not blocked_reason:
        raise ValueError("blocked status requires blocked_reason")
    if state == "failed" and not failure_reason:
        raise ValueError("failed status requires failure_reason")
    payload: dict[str, Any] = {
        "contract": JOB_CONTRACT,
        "job_id": job_id,
        "state": state,
        "observed_at": moment.isoformat(),
        "input_watermark": input_watermark,
        "build_started_at": build_started_at,
        "build_completed_at": build_completed_at,
        "published_at": published_at,
        "stage": stage,
        "progress": progress,
        "blocked_reason": blocked_reason,
        "failure_reason": failure_reason,
        "previous_completed": dict(previous_completed) if previous_completed is not None else None,
    }
    error = _job_status_contract_error(payload, now=moment)
    if error is not None:
        raise ValueError(error)
    atomic_json(path, payload)
    return payload


def _job_status_contract_error(
    payload: Mapping[str, Any], *, now: datetime, expected_job_id: str | None = None
) -> str | None:
    """Validate lifecycle values both before publication and when consumed."""
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        return "job_id is missing"
    if expected_job_id is not None and job_id != expected_job_id:
        return f"job_id mismatch:{job_id}"
    observed = _aware_utc(payload.get("observed_at"))
    if observed is None:
        return "observed_at must be timezone-aware"
    if (observed - now.astimezone(UTC)).total_seconds() > MAX_CLOCK_SKEW_SECONDS:
        return "observed_at is in the future"
    state = payload.get("state")
    if state not in JOB_STATES:
        return f"unsupported job state: {state}"
    parsed: dict[str, datetime | None] = {}
    for name in ("build_started_at", "build_completed_at", "published_at"):
        value = payload.get(name)
        parsed_value = _aware_utc(value)
        parsed[name] = parsed_value
        if value is not None and parsed_value is None:
            return f"{name} must be timezone-aware"
        if (
            parsed_value is not None
            and (parsed_value - now.astimezone(UTC)).total_seconds() > MAX_CLOCK_SKEW_SECONDS
        ):
            return f"{name} is in the future"
    if state in {"running", "failed", "completed"} and parsed["build_started_at"] is None:
        return f"{state} status requires build_started_at"
    if state in {"failed", "completed"} and parsed["build_completed_at"] is None:
        return f"{state} status requires build_completed_at"
    if state == "completed" and parsed["published_at"] is None:
        return "completed status requires published_at"
    if state == "blocked" and not payload.get("blocked_reason"):
        return "blocked status requires blocked_reason"
    if state == "failed" and not payload.get("failure_reason"):
        return "failed status requires failure_reason"
    started = parsed["build_started_at"]
    completed = parsed["build_completed_at"]
    published = parsed["published_at"]
    if started is not None and completed is not None and completed < started:
        return "build_completed_at precedes build_started_at"
    if completed is not None and published is not None and published < completed:
        return "published_at precedes build_completed_at"
    return None


def evaluate_input_watermarks(
    input_watermark: object,
    *,
    now: datetime,
    required_sources: Sequence[str] | None = None,
) -> dict[str, list[str]]:
    """Re-evaluate frozen logical inputs at serve time, never generated_at."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("watermark evaluation now must be timezone-aware")
    if not isinstance(input_watermark, Mapping):
        return {"input_watermark": ["input_watermark_missing"]}
    current = now.astimezone(UTC)
    market_open = is_market_open(current)
    stale: dict[str, list[str]] = {}
    selected = tuple(required_sources) if required_sources is not None else tuple(input_watermark)
    for source_id in selected:
        maximum_age = SOURCE_MAX_AGE_SECONDS.get(source_id)
        if maximum_age is None:
            stale[source_id] = ["unknown_source_contract"]
            continue
        if source_id == "dashboard_state" or source_id not in input_watermark:
            if source_id != "dashboard_state":
                stale[source_id] = ["watermark_unavailable"]
            continue
        watermark = _aware_utc(input_watermark.get(source_id))
        reasons: list[str] = []
        if watermark is None:
            reasons.append("watermark_unavailable")
        elif (watermark - current).total_seconds() > MAX_CLOCK_SKEW_SECONDS:
            reasons.append("future_watermark")
        elif source_id in MARKET_SENSITIVE_SOURCES and not market_open:
            reasons.append("market_closed")
        elif (current - watermark).total_seconds() > maximum_age:
            reasons.append("source_watermark_stale")
        if reasons:
            stale[source_id] = reasons
    return stale


def publish_progress(
    stage: str,
    *,
    status: str = "running",
    failure_reason: str | None = None,
    identities: Mapping[str, object] | None = None,
    teachers: Mapping[str, object] | None = None,
) -> None:
    """Best-effort instrumentation; never change research/risk decisions."""
    target = os.environ.get("FX_JOB_PROGRESS_PATH")
    run_id = os.environ.get("FX_JOB_RUN_ID")
    if not target or not run_id:
        return
    path = Path(target)
    try:
        try:
            payload = read_small_json(path)
        except (OSError, ValueError):
            payload = {}
        if payload.get("run_id") != run_id:
            payload = {"contract": CONTRACT, "run_id": run_id, "identities": {}}
        payload.update(stage=stage, stage_status=status, last_progress_at=utc_now())
        if failure_reason:
            payload["failure_reason"] = failure_reason[:500]
        if identities:
            raw_identities = payload.setdefault("identities", {})
            if not isinstance(raw_identities, dict):
                raw_identities = {}
                payload["identities"] = raw_identities
            raw_identities.update(
                {key: str(value)[:500] for key, value in identities.items() if value is not None}
            )
        if teachers is not None:
            payload["teachers"] = dict(teachers)
        atomic_json(path, payload)
    except (OSError, ValueError, TypeError):
        print("runtime progress write failed", file=__import__("sys").stderr)


_PRODUCERS: tuple[tuple[str, str, str | None], ...] = (
    ("snapshot", "com.fx-codex.snapshot", "tf_prices"),
    ("briefing", "com.fx-codex.briefing", "briefing_decisions"),
    ("health", "com.fx-codex.health", "freshness_monitor"),
    ("dukascopy_history", "com.fx-codex.dukascopy-history", "dukascopy_quotes"),
    ("quote_index", "com.fx-codex.quote-index", "quote_index"),
    ("virtual_portfolio", "com.fx-codex.virtual-portfolio", "virtual_portfolio"),
    ("dashboard_state", "com.fx-codex.dashboard-state", "dashboard_state"),
    ("counterfactual", "com.fx-codex.virtual-portfolio-counterfactual", None),
    ("challenge_learning", "com.fx-codex.virtual-portfolio-challenge-learning", None),
    ("operational_sync", "com.fx-codex.operational-sync", None),
    ("operational_replay", "com.fx-codex.operational-replay", None),
    ("postprocess", "com.fx-codex.virtual-portfolio-postprocess", None),
)
_DISABLED_T1_REASON = "disabled_by_t1_heavy_workload_containment"
_T1_CONTAINED_LABELS = frozenset(
    {
        "com.fx-codex.virtual-portfolio-counterfactual",
        "com.fx-codex.virtual-portfolio-challenge-learning",
        "com.fx-codex.operational-sync",
        "com.fx-codex.operational-replay",
        "com.fx-codex.virtual-portfolio-postprocess",
    }
)
_WRAPPED_PRODUCERS = frozenset(
    {
        "snapshot",
        "briefing",
        "health",
        "dukascopy_history",
        "quote_index",
        "virtual_portfolio",
        "dashboard_state",
    }
)


def _latest_jsonl(path: Path, timestamp_keys: Sequence[str]) -> dict[str, Any]:
    """Return newest parseable tail row having one requested timestamp."""
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - MAX_TAIL_BYTES)
        handle.seek(start)
        raw = handle.read(MAX_TAIL_BYTES)
    lines = raw.splitlines()
    if start and lines:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(row, dict) and any(_aware_utc(row.get(key)) for key in timestamp_keys):
            return row
    raise ValueError("no timestamped record in bounded tail")


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        return None


def _artifact_available(path: Path) -> bool:
    """Check bounded metadata only; never read a potentially heavy artifact."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _last_completed_lifecycle(path: Path, *, job_id: str) -> Mapping[str, object]:
    """Read the last successful lifecycle without treating a newer attempt as output."""
    state = read_small_json(path)
    if state.get("contract") != JOB_CONTRACT or state.get("job_id") != job_id:
        raise ValueError("lifecycle identity mismatch")
    candidate: object = state
    if state.get("state") != "completed":
        candidate = state.get("previous_completed")
    if not isinstance(candidate, Mapping):
        raise ValueError("completed lifecycle unavailable")
    started = _aware_utc(candidate.get("build_started_at"))
    completed = _aware_utc(candidate.get("build_completed_at"))
    published = _aware_utc(candidate.get("published_at"))
    if started is None or completed is None or published is None:
        raise ValueError("completed lifecycle timing unavailable")
    if completed < started or published < completed:
        raise ValueError("completed lifecycle timing invalid")
    if not isinstance(candidate.get("input_watermark"), Mapping):
        raise ValueError("completed lifecycle input watermark unavailable")
    return candidate


def _read_source_inputs(log_dir: Path) -> list[dict[str, Any]]:
    root = log_dir.parent
    sources: list[dict[str, Any]] = []

    def add(
        source_id: str,
        producer_id: str,
        *,
        watermark_at: object,
        input_watermark: object,
        published_at: object,
        max_age_seconds: float,
        market_sensitive: bool = False,
        dependencies: Sequence[str] = (),
        evidence: Mapping[str, object] | None = None,
        read_error: str | None = None,
    ) -> None:
        sources.append(
            {
                "source_id": source_id,
                "producer_id": producer_id,
                "watermark_at": str(watermark_at) if watermark_at is not None else None,
                "input_watermark": input_watermark,
                "published_at": str(published_at) if published_at is not None else None,
                "max_age_seconds": max_age_seconds,
                "market_sensitive": market_sensitive,
                "dependencies": list(dependencies),
                "evidence": dict(evidence or {}),
                "read_error": read_error,
            }
        )

    prices_path = log_dir / "briefing_tf_prices.jsonl"
    try:
        row = _latest_jsonl(prices_path, ("available_time", "ingested_time", "event_time", "ts"))
        available = row.get("available_time") or row.get("ingested_time")
        event = row.get("event_time") or row.get("ts")
        add(
            "tf_prices",
            "snapshot",
            watermark_at=available or event,
            input_watermark={"event_at": event, "available_at": available},
            published_at=_mtime(prices_path),
            max_age_seconds=900,
            market_sensitive=True,
            evidence={"path": str(prices_path), "source_record_id": row.get("source_record_id")},
        )
    except (OSError, ValueError):
        add(
            "tf_prices",
            "snapshot",
            watermark_at=None,
            input_watermark=None,
            published_at=_mtime(prices_path),
            max_age_seconds=900,
            market_sensitive=True,
            evidence={"path": str(prices_path)},
            read_error="watermark_unavailable",
        )
    decisions_path = log_dir / "briefing_tf_journal.jsonl"
    briefing_status_path = log_dir / "runtime_status" / "briefing.json"
    try:
        row = _latest_jsonl(
            decisions_path,
            ("committed_at", "prediction_time", "max_feature_available_time", "ts"),
        )
        completed_state = _last_completed_lifecycle(briefing_status_path, job_id="briefing")
        consumed = completed_state["input_watermark"]
        assert isinstance(consumed, Mapping)
        committed = row.get("committed_at") or row.get("prediction_time") or row.get("ts")
        add(
            "briefing_decisions",
            "briefing",
            watermark_at=committed,
            input_watermark=dict(consumed),
            published_at=completed_state.get("published_at"),
            max_age_seconds=1800,
            dependencies=("tf_prices",),
            evidence={
                "path": str(decisions_path),
                "lifecycle_path": str(briefing_status_path),
                "event_type": row.get("event_type"),
                "journal_published_at": _mtime(decisions_path),
                "watermark_basis": "last_successful_cycle_input_watermark",
            },
        )
    except (OSError, ValueError):
        add(
            "briefing_decisions",
            "briefing",
            watermark_at=None,
            input_watermark=None,
            published_at=None,
            max_age_seconds=1800,
            dependencies=("tf_prices",),
            evidence={
                "path": str(decisions_path),
                "lifecycle_path": str(briefing_status_path),
                "journal_published_at": _mtime(decisions_path),
            },
            read_error="consumed_watermark_unavailable",
        )

    duka_path = root / "collect" / "state" / "dukascopy_history.json"
    try:
        state = read_small_json(duka_path)
        completed = state.get("completed_hours")
        values = list(completed.values()) if isinstance(completed, Mapping) else []
        watermark = max((str(value) for value in values if _aware_utc(value)), default=None)
        add(
            "dukascopy_quotes",
            "dukascopy_history",
            watermark_at=watermark,
            input_watermark={"completed_hours": completed},
            published_at=state.get("heartbeat_at") or _mtime(duka_path),
            max_age_seconds=10800,
            market_sensitive=True,
            evidence={"path": str(duka_path), "heartbeat_ok": state.get("heartbeat_ok")},
            read_error=None if state.get("heartbeat_ok") is True else "heartbeat_not_ok",
        )
    except (OSError, ValueError):
        add(
            "dukascopy_quotes",
            "dukascopy_history",
            watermark_at=None,
            input_watermark=None,
            published_at=_mtime(duka_path),
            max_age_seconds=10800,
            market_sensitive=True,
            evidence={"path": str(duka_path)},
            read_error="status_unavailable",
        )
    quote_index_path = log_dir / "quote_tape_index_status.json"
    quote_index_status_path = log_dir / "runtime_status" / "quote_index.json"
    quote_tape_path = root / "collect" / "log" / "quotes.jsonl"
    try:
        state = read_small_json(quote_index_path)
        completed_state = _last_completed_lifecycle(quote_index_status_path, job_id="quote_index")
        consumed = completed_state["input_watermark"]
        assert isinstance(consumed, Mapping)
        indexed_watermark = consumed.get("dukascopy_quotes")
        indexed, source_size = state.get("indexed_offset"), state.get("source_size_bytes")
        live_source_size = quote_tape_path.stat().st_size
        caught_up = (
            state.get("ok") is True
            and isinstance(indexed, int)
            and isinstance(source_size, int)
            and indexed == source_size == live_source_size
        )
        add(
            "quote_index",
            "quote_index",
            watermark_at=indexed_watermark,
            input_watermark=dict(consumed),
            published_at=completed_state.get("published_at"),
            max_age_seconds=10800,
            dependencies=("dukascopy_quotes",),
            evidence={
                "path": str(quote_index_path),
                "lifecycle_path": str(quote_index_status_path),
                "quote_tape_path": str(quote_tape_path),
                "caught_up": caught_up,
                "indexed_offset": indexed,
                "source_size_bytes": source_size,
                "live_source_size_bytes": live_source_size,
                "integrity_check": state.get("integrity_check"),
                "index_status_published_at": state.get("updated_at") or _mtime(quote_index_path),
                "watermark_basis": "last_successful_cycle_input_watermark",
            },
            read_error=None if caught_up else "index_not_caught_up",
        )
    except (OSError, ValueError):
        add(
            "quote_index",
            "quote_index",
            watermark_at=None,
            input_watermark=None,
            published_at=None,
            max_age_seconds=10800,
            dependencies=("dukascopy_quotes",),
            evidence={
                "path": str(quote_index_path),
                "lifecycle_path": str(quote_index_status_path),
                "quote_tape_path": str(quote_tape_path),
                "index_status_published_at": _mtime(quote_index_path),
            },
            read_error="status_unavailable",
        )

    health_path = log_dir / "freshness_report.json"
    try:
        state = read_small_json(health_path)
        targets = state.get("targets")
        watermarks = (
            {
                str(row.get("name")): row.get("last_update")
                for row in targets
                if isinstance(row, Mapping) and row.get("name")
            }
            if isinstance(targets, list)
            else {}
        )
        add(
            "freshness_monitor",
            "health",
            watermark_at=state.get("monitor_timestamp"),
            input_watermark=watermarks,
            published_at=state.get("monitor_timestamp") or _mtime(health_path),
            max_age_seconds=900,
            evidence={"path": str(health_path), "reported_overall": state.get("overall")},
            read_error=None if state.get("overall") == "ok" else f"reported_{state.get('overall')}",
        )
    except (OSError, ValueError):
        add(
            "freshness_monitor",
            "health",
            watermark_at=None,
            input_watermark=None,
            published_at=_mtime(health_path),
            max_age_seconds=900,
            evidence={"path": str(health_path)},
            read_error="status_unavailable",
        )

    portfolio_path = log_dir / "fx_virtual_portfolio_canonical_v2.sqlite3"
    portfolio_status_path = log_dir / "runtime_status" / "virtual_portfolio.json"
    try:
        completed_state = _last_completed_lifecycle(
            portfolio_status_path, job_id="virtual_portfolio"
        )
        consumed = completed_state["input_watermark"]
        assert isinstance(consumed, Mapping)
        consumed_map = dict(consumed)
        portfolio_watermark = consumed_map.get("briefing_decisions")
        portfolio_available = _artifact_available(portfolio_path)
        add(
            "virtual_portfolio",
            "virtual_portfolio",
            watermark_at=portfolio_watermark,
            input_watermark=dict(consumed_map),
            published_at=completed_state.get("published_at"),
            max_age_seconds=900,
            dependencies=("briefing_decisions", "dukascopy_quotes"),
            evidence={
                "path": str(portfolio_status_path),
                "database_path": str(portfolio_path),
                "database_published_at": _mtime(portfolio_path),
                "artifact_available": portfolio_available,
                "watermark_basis": "last_successful_cycle_input_watermark",
            },
            read_error=(
                "consumed_watermark_unavailable"
                if portfolio_watermark is None
                else None if portfolio_available else "artifact_missing_or_empty"
            ),
        )
    except (OSError, ValueError):
        add(
            "virtual_portfolio",
            "virtual_portfolio",
            watermark_at=None,
            input_watermark=None,
            published_at=None,
            max_age_seconds=900,
            dependencies=("briefing_decisions", "dukascopy_quotes"),
            evidence={
                "path": str(portfolio_status_path),
                "database_path": str(portfolio_path),
                "database_published_at": _mtime(portfolio_path),
            },
            read_error="lifecycle_status_unavailable",
        )

    dashboard_status_path = log_dir / "runtime_status" / "dashboard_state.json"
    cache_path = log_dir / "dashboard_state_cache.json"
    try:
        completed_state = _last_completed_lifecycle(dashboard_status_path, job_id="dashboard_state")
        cache_available = _artifact_available(cache_path)
        add(
            "dashboard_state",
            "dashboard_state",
            watermark_at=completed_state.get("published_at"),
            input_watermark=completed_state.get("input_watermark"),
            published_at=completed_state.get("published_at"),
            max_age_seconds=2100,
            dependencies=("tf_prices", "briefing_decisions", "virtual_portfolio"),
            evidence={
                "path": str(dashboard_status_path),
                "artifact_path": str(cache_path),
                "artifact_published_at": _mtime(cache_path),
                "artifact_available": cache_available,
                "watermark_basis": "last_successful_cycle_input_watermark",
            },
            read_error=None if cache_available else "artifact_missing_or_empty",
        )
    except (OSError, ValueError):
        add(
            "dashboard_state",
            "dashboard_state",
            watermark_at=None,
            input_watermark=None,
            published_at=_mtime(cache_path),
            max_age_seconds=2100,
            dependencies=("tf_prices", "briefing_decisions", "virtual_portfolio"),
            evidence={"path": str(dashboard_status_path)},
            read_error="lifecycle_status_unavailable",
        )
    return sources


def _launchctl_observations(labels: Sequence[str], now: datetime) -> dict[str, dict[str, Any]]:
    observations: dict[str, dict[str, Any]] = {}
    domain = f"gui/{os.getuid()}"
    try:
        result = subprocess.run(
            ["launchctl", "print-disabled", domain],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        disabled_text = result.stdout
        disabled_error = None if result.returncode == 0 else "print_disabled_failed"
    except (OSError, subprocess.TimeoutExpired):
        disabled_text = ""
        disabled_error = "print_disabled_unavailable"

    def inspect(label: str) -> tuple[str, dict[str, Any]]:
        disabled = (
            re.search(rf'"{re.escape(label)}"\s*=>\s*(true|disabled)', disabled_text) is not None
        )
        try:
            result = subprocess.run(
                ["launchctl", "print", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            text, loaded = result.stdout, result.returncode == 0
            observation_error = None
        except subprocess.TimeoutExpired:
            text, loaded = "", None
            observation_error = "launchctl_timeout"
        except OSError:
            text, loaded = "", None
            observation_error = "launchctl_unavailable"
        state_match = re.search(r"^\s*state = ([^\n]+)", text, re.MULTILINE)
        exit_match = re.search(r"^\s*last exit code = (-?\d+)", text, re.MULTILINE)
        pid_match = re.search(r"^\s*pid = (\d+)", text, re.MULTILINE)
        return label, {
            "loaded": loaded,
            "disabled": disabled,
            "disabled_observation_error": disabled_error,
            "observation_error": observation_error,
            "launchd_state": state_match.group(1).strip() if state_match else None,
            "last_exit_code": int(exit_match.group(1)) if exit_match else None,
            "pid": int(pid_match.group(1)) if pid_match else None,
            "observed_at": now.isoformat(),
        }

    with ThreadPoolExecutor(max_workers=min(12, max(1, len(labels)))) as executor:
        observations.update(dict(executor.map(inspect, labels)))
    return observations


def _classify_job(
    label: str, observation: Mapping[str, Any]
) -> tuple[JobState, str | None, str | None]:
    if observation.get("observation_error"):
        return (
            "blocked",
            f"launchd_observation_unavailable:{observation['observation_error']}",
            None,
        )
    if observation.get("disabled") is True:
        reason = (
            _DISABLED_T1_REASON if label in _T1_CONTAINED_LABELS else "launchd_service_disabled"
        )
        return "blocked", reason, None
    if observation.get("loaded") is not True:
        return "blocked", "launchd_service_not_loaded", None
    if observation.get("launchd_state") == "running":
        return "running", None, None
    exit_code = observation.get("last_exit_code")
    if exit_code == 75:
        return "blocked", "shared_workload_slot_busy", None
    if isinstance(exit_code, int) and exit_code != 0:
        return "failed", None, f"last_exit_code:{exit_code}"
    return "completed", None, None


def collect_input_watermarks(log_dir: Path) -> dict[str, object]:
    """Capture bounded logical source watermarks before a build starts."""
    return {row["source_id"]: row["watermark_at"] for row in _read_source_inputs(log_dir)}


def build_runtime_status(
    log_dir: Path,
    *,
    now: datetime | None = None,
    service_observations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build status without reading heavy dashboard/research aggregates."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("runtime status now must be timezone-aware")
    moment = moment.astimezone(UTC)
    log_dir = log_dir.expanduser().resolve()
    sources = _read_source_inputs(log_dir)
    labels = [label for _, label, _ in _PRODUCERS]
    observations = (
        {key: dict(value) for key, value in service_observations.items()}
        if service_observations is not None
        else _launchctl_observations(labels, moment)
    )
    source_by_id = {row["source_id"]: row for row in sources}
    producer_states: dict[str, JobState] = {}
    producer_blocked_reasons: dict[str, str | None] = {}
    jobs: list[dict[str, Any]] = []
    for producer_id, label, source_id in _PRODUCERS:
        observation = observations.get(label, {})
        state, blocked_reason, failure_reason = _classify_job(label, observation)
        source = source_by_id.get(source_id or "")
        lifecycle_path = log_dir / "runtime_status" / f"{producer_id}.json"
        try:
            lifecycle = read_small_json(lifecycle_path)
            lifecycle_error = (
                "contract_mismatch"
                if lifecycle.get("contract") != JOB_CONTRACT
                else _job_status_contract_error(lifecycle, now=moment, expected_job_id=producer_id)
            )
        except FileNotFoundError:
            lifecycle = {}
            lifecycle_error = (
                "lifecycle_status_missing" if producer_id in _WRAPPED_PRODUCERS else None
            )
        except (OSError, ValueError) as error:
            lifecycle = {}
            lifecycle_error = f"unreadable:{type(error).__name__}"
        if lifecycle_error is not None:
            lifecycle = {}
            state = "failed"
            blocked_reason = None
            failure_reason = f"invalid_lifecycle_status:{lifecycle_error}"
        if lifecycle and state != "blocked":
            lifecycle_state = lifecycle.get("state")
            if state == "running" and lifecycle_state != "running":
                lifecycle = {}
                state = "failed"
                failure_reason = "running_without_current_lifecycle"
            elif state == "completed" and lifecycle_state == "running":
                lifecycle = {}
                state = "failed"
                failure_reason = "orphaned_running_lifecycle"
            elif state == "completed" and lifecycle_state in JOB_STATES:
                state = lifecycle_state
                blocked_reason = lifecycle.get("blocked_reason")
                failure_reason = lifecycle.get("failure_reason")
            elif state == "running":
                blocked_reason = None
                failure_reason = None
            elif state == "failed" and lifecycle_state == "failed":
                failure_reason = lifecycle.get("failure_reason")
        producer_states[producer_id] = state
        producer_blocked_reasons[producer_id] = blocked_reason
        published = (
            lifecycle.get("published_at") if lifecycle else (source or {}).get("published_at")
        )
        completed = (
            lifecycle.get("build_completed_at")
            if lifecycle
            else published if state == "completed" else None
        )
        jobs.append(
            {
                "job_id": producer_id,
                "service_label": label,
                "state": state,
                "stage": lifecycle.get("stage")
                or (
                    "building"
                    if state == "running"
                    else "idle_between_scheduled_runs" if state == "completed" else state
                ),
                "progress": lifecycle.get("progress"),
                "input_watermark": (
                    lifecycle.get("input_watermark")
                    if lifecycle
                    else (source or {}).get("input_watermark")
                ),
                "build_started_at": lifecycle.get("build_started_at"),
                "build_completed_at": completed,
                "published_at": published,
                "blocked_reason": blocked_reason,
                "failure_reason": failure_reason,
                "launchd": dict(observation),
            }
        )

    evaluated: dict[str, dict[str, Any]] = {}
    market_open = is_market_open(moment)
    for source in sources:
        watermark = _aware_utc(source.get("watermark_at"))
        age = (moment - watermark).total_seconds() if watermark else None
        reasons: list[str] = []
        producer_state = producer_states.get(str(source["producer_id"]), "blocked")
        if producer_state == "blocked":
            if str(producer_blocked_reasons.get(str(source["producer_id"]))).startswith(
                "launchd_observation_unavailable"
            ):
                reasons.append("producer_observation_unavailable")
            else:
                reasons.append("producer_blocked")
        elif producer_state == "failed":
            reasons.append("producer_failed")
        if source.get("read_error"):
            reasons.append(str(source["read_error"]))
        if watermark is None:
            reasons.append("watermark_unavailable")
        elif age is not None and age < -60:
            reasons.append("future_watermark")
        if source.get("market_sensitive") and not market_open:
            reasons.append("market_closed")
        elif age is not None and age > float(source["max_age_seconds"]):
            reasons.append("source_watermark_stale")
        for dependency in source["dependencies"]:
            consumed_stale = evaluate_input_watermarks(
                source.get("input_watermark"),
                now=moment,
                required_sources=(str(dependency),),
            )
            for consumed_reason in consumed_stale.get(str(dependency), []):
                reasons.append(f"consumed_input_stale:{dependency}:{consumed_reason}")
            upstream = evaluated.get(str(dependency))
            if upstream is not None and upstream["stale"]:
                reasons.append(
                    f"upstream_stale:{dependency}:{upstream.get('stale_reason') or 'unknown'}"
                )
        unique_reasons = list(dict.fromkeys(reasons))
        result = dict(source)
        result.update(
            {
                "age_seconds": round(age, 3) if age is not None else None,
                "producer_state": producer_state,
                "market_status": "open" if market_open else "closed",
                "producer_stale_reason": next(
                    (reason for reason in unique_reasons if reason.startswith("producer_")), None
                ),
                "market_stale_reason": (
                    "market_closed" if "market_closed" in unique_reasons else None
                ),
                "stale": bool(unique_reasons),
                "stale_reason": unique_reasons[0] if unique_reasons else None,
                "stale_reasons": unique_reasons,
                "consumer_eligible": not unique_reasons,
            }
        )
        evaluated[str(source["source_id"])] = result

    stale_sources = [row["source_id"] for row in evaluated.values() if row["stale"]]
    unhealthy_jobs = [row["job_id"] for row in jobs if row["state"] in {"blocked", "failed"}]
    return {
        "contract": CONTRACT,
        "generated_at": moment.isoformat(),
        "generated_at_semantics": "observation_time_only_not_a_freshness_watermark",
        "bounded_read_max_bytes": MAX_TAIL_BYTES,
        "market": {
            "status": "open" if market_open else "closed",
            "observed_at": moment.isoformat(),
        },
        "overall": "degraded" if stale_sources or unhealthy_jobs else "ok",
        "consumer_eligible": not stale_sources,
        "stale_sources": stale_sources,
        "unhealthy_jobs": unhealthy_jobs,
        "jobs": jobs,
        "sources": list(evaluated.values()),
    }


__all__ = [
    "CONTRACT",
    "DASHBOARD_INPUT_SOURCES",
    "JOB_CONTRACT",
    "JOB_STATES",
    "atomic_json",
    "build_runtime_status",
    "collect_input_watermarks",
    "evaluate_input_watermarks",
    "publish_job_status",
    "publish_progress",
    "read_small_json",
    "utc_now",
]
