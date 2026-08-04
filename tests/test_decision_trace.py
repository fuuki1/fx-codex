"""Decision Trace Analytics MVP のテスト。

重点は「読み取り専用であること」「壊れた行を黙って捨てないこと」
「相互排他ゲートと独立ゲートを取り違えないこと」、および
旧実装(層別なし単純集計)では検出できない異常を新実装が検出できることの逆検証。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from fx_intel.decision_trace import (
    EXCLUSIVE_GATES,
    INDEPENDENT_GATES,
    FunnelReconciliationError,
    build_report,
    parse_event,
    report_to_dict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "fx_intel" / "decision_trace.py"
CLI_PATH = REPO_ROOT / "tools" / "decision_trace_report.py"


def _event(
    decision_id: str,
    *,
    pair: str = "USDJPY",
    timeframe: str = "1h",
    regime: str = "trend",
    direction: str = "long",
    blocked: list[str] | None = None,
    ts: str = "2026-08-04T00:00:00+00:00",
    include_liquidity: bool = False,
) -> dict[str, Any]:
    """実コードの構造(timeframe.py:625-641)に合わせた判断イベントを作る。"""
    blocked = blocked or []
    gate_trace: list[dict[str, Any]] = [{"gate": name, "status": "blocked"} for name in blocked]
    if include_liquidity:
        # input_context.py:488-498 と同じ形。status="observed" でブロックではない。
        gate_trace.append(
            {
                "gate": "liquidity",
                "policy_version": "v1",
                "stage": "shadow",
                "input_status": "thin",
                "would_block": False,
                "would_dampen": True,
                "applied": False,
                "reason_codes": [],
                "status": "observed",
            }
        )
    return {
        "decision_id": decision_id,
        "ts": ts,
        "source": "timeframe_raw",
        "mode": "per_timeframe",
        "symbol": pair,
        "timeframe": timeframe,
        "decision": {
            "direction": direction,
            "blocked_by": list(blocked),
            "gate_trace": gate_trace,
            "learning_dimensions": {"regime": regime},
        },
    }


# ---- 旧実装ベースライン(層別なし・単純集計) ------------------------------
# 逆検証8a/8b で「旧実装では検出できない」ことを示すために同ファイル内に置く。


def baseline_simple_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    """旧実装相当: 全体をまとめて数えるだけ。層別も支配率も持たない。"""
    counts: dict[str, int] = {}
    for event in events:
        blocked = event.get("decision", {}).get("blocked_by", [])
        key = blocked[0] if blocked else "passed"
        counts[key] = counts.get(key, 0) + 1
    return counts


def baseline_detects_anomaly(events: list[dict[str, Any]]) -> bool:
    """旧実装の異常検出: 全体の件数分布しか見ないので、偏りを判定できない。

    「どのゲートが最多か」は分かるが、それが異常かを判断する基準を持たない。
    層別しないため、特定セルだけの異常は全体平均に埋もれる。
    """
    counts = baseline_simple_counts(events)
    if not counts:
        return False
    # 旧実装が持っていた唯一の警告条件: 全体で1つのゲートが全件を占める場合のみ。
    total = sum(counts.values())
    return any(count == total for key, count in counts.items() if key != "passed")


# ---- 1. 既存追跡ファイルの変更が0件 ---------------------------------------


def test_no_tracked_files_modified() -> None:
    """追跡済みファイルを1行も変更していないこと。"""
    result = subprocess.run(
        ["git", "diff", "--stat", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", f"追跡ファイルが変更されている:\n{result.stdout}"


# ---- 2. 読み取り専用であることのAST検査 -----------------------------------


def _writing_calls(path: Path) -> list[str]:
    """書き込みAPIの呼び出しを検出する。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    banned_names = {"write", "writelines", "writerow", "writerows", "dump", "unlink", "mkdir"}
    banned_funcs = {"open"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in banned_names:
                found.append(func.attr)
            if func.attr in {"execute", "executemany", "commit"}:
                found.append(func.attr)
        elif isinstance(func, ast.Name) and func.id in banned_funcs:
            found.append(func.id)
    return found


def test_module_has_no_write_api() -> None:
    """解析モジュールが書き込みAPIを一切含まないこと。"""
    assert _writing_calls(MODULE_PATH) == []


def test_module_does_not_import_io_writers() -> None:
    """sqlite3 等の書き込み可能な依存を持ち込んでいないこと。"""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "sqlite3" not in imported
    assert "shutil" not in imported


def test_inputs_unchanged_after_all_public_functions(tmp_path: Path) -> None:
    """全公開関数の実行後に入力fixtureのmtimeと内容が不変であること。"""
    fixture = tmp_path / "events.jsonl"
    events = [_event("d1"), _event("d2", blocked=["expectancy_guard"], direction="neutral")]
    fixture.write_text(
        "\n".join(json.dumps(event) for event in events),
        encoding="utf-8",
    )
    before_bytes = fixture.read_bytes()
    before_mtime = fixture.stat().st_mtime_ns

    parsed = [parse_event(event) for event in events]
    report = build_report(events)
    report_to_dict(report)
    assert parsed

    assert fixture.read_bytes() == before_bytes
    assert fixture.stat().st_mtime_ns == before_mtime


# ---- 3. 壊れた入力が理由付きで indeterminate に残る ------------------------


def test_broken_rows_recorded_as_indeterminate_with_reasons() -> None:
    """壊れたJSON/欠損decision_id/非aware時刻を黙って除外しないこと。"""
    events: list[Any] = [
        None,  # 壊れたJSON行のマーカー
        {"ts": "2026-08-04T00:00:00+00:00", "symbol": "USDJPY", "decision": {}},  # id欠損
        _event("d3", ts="2026-08-04T00:00:00"),  # naive時刻
        _event("d4"),  # 正常
    ]
    report = build_report(events)

    assert report.overall.total_decisions == 4
    assert report.overall.indeterminate == 3
    assert report.overall.passed_all_gates == 1

    reasons = report.parse_reason_counts
    assert reasons["event_not_mapping"] == 1
    assert reasons["missing_decision_id"] == 1
    assert reasons["naive_ts"] == 1


def test_naive_timestamp_is_not_silently_coerced_to_utc() -> None:
    """naive時刻を勝手にUTCとみなさないこと(PIT整合の保護)。"""
    event = parse_event(_event("d1", ts="2026-08-04T00:00:00"))
    assert event.parse_status == "indeterminate"
    assert "naive_ts" in event.parse_reasons
    assert event.ts is None


def test_missing_regime_becomes_unknown_not_invented() -> None:
    """regime欠損は "unknown"。値を捏造しない。"""
    raw = _event("d1")
    raw["decision"]["learning_dimensions"] = {}
    assert parse_event(raw).regime == "unknown"


# ---- 4. ファネル恒等式の破れで送出 ----------------------------------------


def test_funnel_reconciliation_error_on_unclassifiable_event() -> None:
    """分類不能な行を"通過"に落とさず FunnelReconciliationError を送出する。

    未知の parse_status を passed_all_gates に混ぜると恒等式が**偶然成立**して
    誤集計を隠してしまう。fail-closed であることを直接検証する。
    """
    import fx_intel.decision_trace as module

    broken = module.TraceEvent(
        decision_id="d1",
        ts=None,
        pair="USDJPY",
        timeframe="1h",
        producer="p",
        regime="trend",
        final_action="long",
        gates=(),
        parse_status="weird",  # "ok" でも "indeterminate" でもない
        parse_reasons=(),
    )
    with pytest.raises(FunnelReconciliationError):
        module._summarize([broken])


def test_unclassifiable_event_is_not_silently_counted_as_passed() -> None:
    """回帰防止: 分類不能な行が passed_all_gates に混入しないこと。

    実装が fail-closed でなければ、この行は"通過"として黙って計上され、
    恒等式が偶然成立するため異常が永久に隠れる。
    """
    import fx_intel.decision_trace as module

    ok_event = parse_event(_event("d1"))
    broken = module.TraceEvent(
        decision_id="d2",
        ts=None,
        pair="USDJPY",
        timeframe="1h",
        producer="p",
        regime="trend",
        final_action="long",
        gates=(),
        parse_status="weird",
        parse_reasons=(),
    )
    # 正常行だけなら通る。
    assert module._summarize([ok_event]).passed_all_gates == 1
    # 分類不能行が混ざった瞬間に送出する(passedに足さない)。
    with pytest.raises(FunnelReconciliationError):
        module._summarize([ok_event, broken])


def test_funnel_identity_holds_on_mixed_input() -> None:
    """正常系で恒等式が常に成立すること。"""
    events = [
        _event("d1"),
        _event("d2", blocked=["event_window"], direction="standby"),
        _event("d3", blocked=["expectancy_guard"], direction="neutral"),
        None,
    ]
    summary = build_report(events).overall
    assert (
        summary.passed_all_gates + sum(summary.blocked_at.values()) + summary.indeterminate
        == summary.total_decisions
    )


# ---- 5. 決定論性 -----------------------------------------------------------


def test_report_is_deterministic_across_runs() -> None:
    """2回実行してバイト列一致すること。"""
    events = [
        _event("d1", pair="EURUSD", timeframe="15m", regime="range"),
        _event("d2", blocked=["market_closed"], direction="closed"),
        _event("d3", blocked=["expectancy_guard", "missing_atr"], direction="neutral"),
        _event("d4", include_liquidity=True),
    ]
    first = json.dumps(report_to_dict(build_report(events)), sort_keys=True).encode()
    second = json.dumps(report_to_dict(build_report(events)), sort_keys=True).encode()
    assert first == second


def test_report_independent_of_input_dict_ordering() -> None:
    """dict順序に依存しないこと。"""
    event = _event("d1", blocked=["expectancy_guard"], direction="neutral")
    reordered = {key: event[key] for key in reversed(list(event.keys()))}
    assert json.dumps(report_to_dict(build_report([event])), sort_keys=True) == json.dumps(
        report_to_dict(build_report([reordered])), sort_keys=True
    )


# ---- 6. liquidity(observed)が blocked に混入しない ------------------------


def test_liquidity_observed_never_counted_as_blocked() -> None:
    """liquidityのobservedは blocked にも indeterminate にも混入しない。"""
    event = parse_event(_event("d1", include_liquidity=True))
    assert event.parse_status == "ok"
    assert event.blocked_gates == ()
    assert event.observed_gates == ("liquidity",)
    assert event.first_blocker is None

    summary = build_report([_event("d1", include_liquidity=True)]).overall
    assert summary.passed_all_gates == 1
    assert summary.blocked_at == {}
    assert "liquidity" not in summary.blocked_at


def test_liquidity_alongside_real_block_does_not_shift_first_blocker() -> None:
    """observed行があっても first_blocker は blocked の先頭のまま。"""
    event = parse_event(
        _event("d1", blocked=["event_window"], direction="standby", include_liquidity=True)
    )
    assert event.first_blocker == "event_window"
    assert event.observed_gates == ("liquidity",)


# ---- 7. expectancy_guard + missing_atr の共起は合法(偽陽性の回帰防止) -----


def test_expectancy_guard_and_missing_atr_cooccurrence_is_legal() -> None:
    """独立ifの共起を不正扱いしないこと。ここを誤ると偽陽性を量産する。"""
    summary = build_report(
        [_event("d1", blocked=["expectancy_guard", "missing_atr"], direction="neutral")]
    ).overall
    assert summary.invalid_transitions == ()
    assert summary.blocked_at == {"expectancy_guard": 1}


def test_independent_gate_with_exclusive_gate_is_legal() -> None:
    """排他ゲート1つ + 独立ゲートの組み合わせも合法。"""
    summary = build_report(
        [_event("d1", blocked=["event_window", "expectancy_guard"], direction="standby")]
    ).overall
    assert summary.invalid_transitions == ()


def test_two_exclusive_gates_are_flagged_invalid() -> None:
    """相互排他ゲートの同時出現のみを不正遷移とする。"""
    summary = build_report(
        [_event("d1", blocked=["market_closed", "event_window"], direction="closed")]
    ).overall
    assert len(summary.invalid_transitions) == 1
    assert summary.invalid_transitions[0]["reason"] == "mutually_exclusive_gates_co_occurred"


def test_gate_classification_constants_match_source_semantics() -> None:
    """定数が実コードの分類と一致していること。"""
    assert "expectancy_guard" in INDEPENDENT_GATES
    assert "missing_atr" in INDEPENDENT_GATES
    assert not (EXCLUSIVE_GATES & INDEPENDENT_GATES)


def test_missing_transition_detected() -> None:
    """neutral かつ blocked_by 空の判断を欠損遷移として検出する。"""
    summary = build_report([_event("d1", direction="neutral")]).overall
    assert len(summary.missing_transitions) == 1
    assert summary.missing_transitions[0]["reason"] == "neutral_without_blocking_gate"


# ---- 8a. 逆検証A: 鮮度自己参照ループ ---------------------------------------


def test_counter_validation_a_freshness_self_reference_loop() -> None:
    """全判断が operational_data_stale で first_blocker 100% の異常。

    旧実装(単純集計)は「最多ゲート」は分かるが支配率という概念を持たないため、
    これが異常であると判定する手段がない。新実装は dominance==1.0 で検出する。
    """
    events = [
        _event(
            f"d{index}",
            blocked=["operational_data_stale"],
            direction="neutral",
            pair="USDJPY" if index % 2 == 0 else "EURUSD",
            timeframe="1h" if index % 2 == 0 else "15m",
        )
        for index in range(20)
    ]

    # --- 新実装: dominance==1.0 として検出できる ---
    summary = build_report(events).overall
    assert summary.dominance["operational_data_stale"] == 1.0
    assert summary.blocked_at["operational_data_stale"] == 20
    assert summary.passed_all_gates == 0

    # --- 旧実装: 支配率を持たないので「件数が多い」以上のことが言えない ---
    baseline = baseline_simple_counts(events)
    assert baseline == {"operational_data_stale": 20}
    assert "dominance" not in baseline
    # 旧実装のベースラインが持つ唯一の警告条件は全件一致のみで、
    # 層別された異常(8b)には無力であることを次のテストで示す。


def test_counter_validation_a_baseline_lacks_dominance_concept() -> None:
    """旧実装には支配率が無く、混在時に鮮度ループを異常と判定できない。"""
    # 鮮度ループ(19件)に正常判断1件が混ざるだけで、旧実装の唯一の条件は崩れる。
    events = [
        _event(f"d{index}", blocked=["operational_data_stale"], direction="neutral")
        for index in range(19)
    ]
    events.append(_event("ok1"))

    assert baseline_detects_anomaly(events) is False  # 旧実装は検出できない

    summary = build_report(events).overall
    assert summary.dominance["operational_data_stale"] == pytest.approx(0.95)
    assert summary.dominance["operational_data_stale"] > 0.9  # 新実装は検出できる


# ---- 8b. 逆検証B: 時間足別guard異常 ----------------------------------------


def test_counter_validation_b_per_timeframe_guard_anomaly() -> None:
    """特定timeframeのみ below_production_threshold が100%、他は正常。

    旧実装(全体平均)では埋もれて検出不能。新実装のtimeframe層別では
    当該セルのみ異常として検出できる。
    """
    events: list[dict[str, Any]] = []
    # 4h だけ全件ブロック(10件)
    for index in range(10):
        events.append(
            _event(
                f"bad{index}",
                timeframe="4h",
                blocked=["below_production_threshold"],
                direction="neutral",
            )
        )
    # 他の時間足は正常(30件)
    for index in range(30):
        events.append(
            _event(
                f"ok{index}",
                timeframe="15m" if index % 2 == 0 else "1h",
                direction="long",
            )
        )

    # --- 旧実装: 全体平均に埋もれて検出不能 ---
    baseline = baseline_simple_counts(events)
    assert baseline["below_production_threshold"] == 10
    assert baseline["passed"] == 30
    # 全体では25%にすぎず、旧実装の警告条件(全件一致)には到達しない。
    assert baseline_detects_anomaly(events) is False

    # --- 新実装: timeframe層別で当該セルのみ異常として検出 ---
    report = build_report(events)
    overall_share = report.overall.dominance.get("below_production_threshold", 0.0)
    assert overall_share == pytest.approx(0.25)  # 全体では埋もれる

    by_tf = report.by_timeframe
    assert by_tf["4h"].dominance["below_production_threshold"] == 1.0  # セルでは100%
    assert by_tf["15m"].blocked_at == {}
    assert by_tf["1h"].blocked_at == {}

    anomalous = [
        name
        for name, summary in by_tf.items()
        if summary.dominance.get("below_production_threshold", 0.0) >= 0.9
    ]
    assert anomalous == ["4h"]


def test_stratification_covers_pair_timeframe_regime() -> None:
    """pair × timeframe × regime の層別が全て存在すること。"""
    events = [
        _event("d1", pair="USDJPY", timeframe="1h", regime="trend"),
        _event("d2", pair="EURUSD", timeframe="15m", regime="range"),
    ]
    report = build_report(events)
    assert set(report.by_pair) == {"USDJPY", "EURUSD"}
    assert set(report.by_timeframe) == {"1h", "15m"}
    assert set(report.by_regime) == {"trend", "range"}
    assert set(report.by_cell) == {"USDJPY|1h|trend", "EURUSD|15m|range"}


# ---- 遅延は測定不能として明示 ----------------------------------------------


def test_gate_level_latency_reported_as_unmeasurable() -> None:
    """gate単位の遅延は推定値を捏造せず measurable=false とする。"""
    report = build_report([_event("d1"), _event("d2", ts="2026-08-04T00:05:00+00:00")])
    per_gate = report.latency["per_gate"]
    assert per_gate["measurable"] is False
    assert per_gate["reason"] == "gate_level_timestamps_absent"
    # 判断単位の分布のみ算出する。
    assert report.latency["per_decision"]["measurable"] is True
    assert report.latency["per_decision"]["span_seconds"] == 300.0


# ---- CLI ------------------------------------------------------------------


def test_cli_writes_only_to_stdout(tmp_path: Path) -> None:
    """CLIがstdoutのみに出力し、入力を変更しないこと。"""
    fixture = tmp_path / "events.jsonl"
    fixture.write_text(json.dumps(_event("d1")) + "\n", encoding="utf-8")
    before = fixture.read_bytes()

    result = subprocess.run(
        ["python3", str(CLI_PATH), "--input", str(fixture), "--format", "json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["contract"] == "decision-trace-analytics-mvp-v1"
    assert payload["read_only"] is True
    assert fixture.read_bytes() == before


def test_cli_has_no_write_mode_open() -> None:
    """CLIがファイル書き込みモードのopenを持たないこと。"""
    source = CLI_PATH.read_text(encoding="utf-8")
    for banned in ('"w"', "'w'", '"a"', "'a'", '"wb"', "'wb'"):
        assert banned not in source
