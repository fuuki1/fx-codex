"""Eval Suite loading, execution, and persistence."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .graders import grade_case
from .schemas import EvalResult, EvalRun, EvalSuite
from agent_os.sessions.schemas import AgentOSValidationError, utc_now_iso
from agent_os.sessions.store import AgentOSStorageError


def load_eval_suite(path: str | Path) -> EvalSuite:
    """Load an EvalSuite.

    The default suite file is named .yaml, but its contents are JSON. JSON is a
    valid YAML subset and avoids adding a YAML dependency in Phase 3.
    """

    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentOSStorageError(f"Unable to read eval suite {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AgentOSStorageError(
            f"Invalid eval suite JSON/YAML-subset in {target}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise AgentOSStorageError(f"Expected object in eval suite {target}")
    return EvalSuite.from_dict(data)


def load_actuals(path: str | Path) -> dict[str, dict[str, Any]]:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentOSStorageError(f"Unable to read eval actuals {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AgentOSStorageError(f"Invalid actuals JSON in {target}: {exc}") from exc
    if isinstance(data, dict) and "cases" in data and isinstance(data["cases"], dict):
        data = data["cases"]
    if not isinstance(data, dict):
        raise AgentOSStorageError("actuals must be an object keyed by case_id")
    actuals: dict[str, dict[str, Any]] = {}
    for case_id, actual in data.items():
        if not isinstance(case_id, str) or not isinstance(actual, dict):
            raise AgentOSStorageError("actuals must map case_id strings to objects")
        actuals[case_id] = actual
    return actuals


class EvalStore:
    """Filesystem store for EvalRun artifacts."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @staticmethod
    def new_eval_run_id(suite_id: str) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", suite_id).strip("_")[:48] or "suite"
        return f"eval_{timestamp}_{slug}"

    def run_dir(self, eval_run_id: str) -> Path:
        self._validate_eval_run_id(eval_run_id)
        return self.root / eval_run_id

    def create_run(
        self,
        suite: EvalSuite,
        *,
        eval_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EvalRun:
        run = EvalRun(
            eval_run_id=eval_run_id or self.new_eval_run_id(suite.suite_id),
            suite_id=suite.suite_id,
            status="running",
            started_at=utc_now_iso(),
            metadata=metadata or {},
        )
        self._ensure_run_dir(run.eval_run_id, "artifacts")
        self.save_run(run)
        self.append_event(run.eval_run_id, "eval_run_created", {"suite_id": suite.suite_id})
        return run

    def save_run(self, run: EvalRun) -> None:
        run = EvalRun.from_dict(run.to_dict())
        self._write_json(run.eval_run_id, "eval_run.json", run.to_dict())

    def load_run(self, eval_run_id: str) -> EvalRun:
        data = self._read_json(eval_run_id, "eval_run.json")
        return EvalRun.from_dict(data)

    def append_result(self, result: EvalResult) -> None:
        EvalResult.from_dict(result.to_dict())
        self._append_jsonl(result.eval_run_id, "case_results.jsonl", result.to_dict())
        self.append_event(
            result.eval_run_id,
            "eval_case_recorded",
            {"case_id": result.case_id, "passed": result.passed, "score": result.score},
        )

    def read_results(self, eval_run_id: str) -> list[EvalResult]:
        return [
            EvalResult.from_dict(item)
            for item in self._read_jsonl(eval_run_id, "case_results.jsonl", missing_ok=True)
        ]

    def finish_run(self, run: EvalRun, results: list[EvalResult]) -> EvalRun:
        passed = sum(1 for result in results if result.passed)
        failed = len(results) - passed
        safety_results = [
            result
            for result in results
            if result.metadata.get("category") in {"policy", "safety_policy", "finance_safety"}
        ]
        safety_pass_rate = (
            sum(1 for result in safety_results if result.passed) / len(safety_results)
            if safety_results
            else 1.0
        )
        finished = EvalRun(
            eval_run_id=run.eval_run_id,
            suite_id=run.suite_id,
            status="completed" if failed == 0 else "failed",
            started_at=run.started_at,
            ended_at=utc_now_iso(),
            total=len(results),
            passed=passed,
            failed=failed,
            safety_pass_rate=safety_pass_rate,
            case_results_path=run.case_results_path,
            metadata=dict(run.metadata),
        )
        self.save_run(finished)
        self._write_regressions(finished.eval_run_id, results)
        self.append_event(
            finished.eval_run_id,
            "eval_run_finished",
            {
                "status": finished.status,
                "total": finished.total,
                "passed": finished.passed,
                "failed": finished.failed,
                "safety_pass_rate": finished.safety_pass_rate,
            },
        )
        return finished

    def read_events(self, eval_run_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(eval_run_id, "events.jsonl", missing_ok=True)

    def append_event(self, eval_run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._append_jsonl(
            eval_run_id,
            "events.jsonl",
            {"ts": utc_now_iso(), "event": event_type, "payload": dict(payload)},
        )

    def _write_regressions(self, eval_run_id: str, results: list[EvalResult]) -> None:
        failing = [result for result in results if not result.passed]
        lines = [f"# Eval regressions for {eval_run_id}", ""]
        if not failing:
            lines.append("No failing eval cases.")
        else:
            for result in failing:
                lines.append(f"## {result.case_id}")
                lines.append(f"- score: {result.score:.3f}")
                lines.append(f"- grader: {result.grader_type}")
                for reason in result.reasons:
                    lines.append(f"- reason: {reason}")
                if result.error_summary:
                    lines.append(f"- error: {result.error_summary}")
                lines.append("")
        path = self.run_dir(eval_run_id) / "regressions.md"
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {path}: {exc}") from exc

    def _ensure_run_dir(self, eval_run_id: str, *parts: str) -> Path:
        path = self.run_dir(eval_run_id).joinpath(*parts)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to create {path}: {exc}") from exc
        return path

    def _write_json(self, eval_run_id: str, name: str, data: dict[str, Any]) -> None:
        self._ensure_run_dir(eval_run_id)
        path = self.run_dir(eval_run_id) / name
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to write {path}: {exc}") from exc

    def _read_json(self, eval_run_id: str, name: str) -> dict[str, Any]:
        path = self.run_dir(eval_run_id) / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AgentOSStorageError(f"Invalid JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AgentOSStorageError(f"Expected JSON object in {path}")
        return data

    def _append_jsonl(self, eval_run_id: str, name: str, data: dict[str, Any]) -> None:
        self._ensure_run_dir(eval_run_id)
        path = self.run_dir(eval_run_id) / name
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to append {path}: {exc}") from exc

    def _read_jsonl(self, eval_run_id: str, name: str, *, missing_ok: bool) -> list[dict[str, Any]]:
        path = self.run_dir(eval_run_id) / name
        if missing_ok and not path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AgentOSStorageError(f"Unable to read {path}: {exc}") from exc
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentOSStorageError(f"Invalid JSONL in {path}:{line_no}: {exc}") from exc
            if not isinstance(item, dict):
                raise AgentOSStorageError(f"Expected JSON object in {path}:{line_no}")
            records.append(item)
        return records

    @staticmethod
    def _validate_eval_run_id(eval_run_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", eval_run_id):
            raise AgentOSValidationError("eval_run_id contains unsafe characters")


def run_eval_suite(
    *,
    suite: EvalSuite,
    actuals_by_case: dict[str, dict[str, Any]],
    store: EvalStore,
    eval_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[EvalRun, list[EvalResult]]:
    run = store.create_run(suite, eval_run_id=eval_run_id, metadata=metadata)
    results: list[EvalResult] = []
    for case in suite.cases:
        actual = actuals_by_case.get(case.case_id, {})
        if not actual:
            result = EvalResult(
                result_id=f"eval_result_missing_{case.case_id}",
                eval_run_id=run.eval_run_id,
                case_id=case.case_id,
                grader_type=str(case.grader.get("type")),
                passed=False,
                score=0.0,
                reasons=["missing actuals for case"],
                metadata={"category": case.category, "role": case.role},
            )
        else:
            result = grade_case(case, actual, eval_run_id=run.eval_run_id)
        store.append_result(result)
        results.append(result)
    return store.finish_run(run, results), results
