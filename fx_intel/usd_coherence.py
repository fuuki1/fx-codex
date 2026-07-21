"""同時提示された判断群のUSDファクター整合監査(観測専用)。

背景(2026-07-16の実測): 3ペア同時にlongが提示された。USDJPY long は「USD強」、
EURUSD/GBPUSD long は「USD弱」の賭けであり、同一実行内でUSD観が内部矛盾していた。
当日はUSD全面高となり、EURUSD/GBPUSDのlongが全敗した(briefing_journal.jsonlで確認)。

この監査は、1回の実行で提示された判断群からペアごとのUSDスタンス
(+1=USD強に賭ける / -1=USD弱に賭ける)を集計し、矛盾の有無・確信度加重の
多数派・少数派(減衰候補)を記録する。

観測専用(stage=shadow): direction / conviction は一切変更しない。liquidityゲートと
同じ運用で、would_dampen は「有効化した場合の提案」をgate_traceへ残すだけ。
実際の減衰は、蓄積した観測で提案の期待値改善が独立レビューで確認されてから
別PRで有効化する(データ・リスク由来の既存vetoを上書きする用途には使わない)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

POLICY_VERSION = "usd-factor-coherence-v1"
GATE_NAME = "usd_factor_coherence"
# 有効化時の減衰案。観測段階では適用されず、gate_traceの提案値としてだけ残る
DAMPEN_FACTOR_PROPOSAL = 0.75


def usd_stance(symbol: str, direction: object) -> int:
    """判断1件のUSDスタンス。+1=USD強に賭ける / -1=USD弱 / 0=対象外。

    USDが基軸(USDJPY等)ならlong=+1、USDがクォート(EURUSD等)ならlong=-1。
    USDを含まないクロス(EURJPY等)と無方向判断は0。
    """
    if direction not in ("long", "short"):
        return 0
    cleaned = str(symbol).upper().replace("/", "")
    sign = 1 if direction == "long" else -1
    if cleaned.startswith("USD"):
        return sign
    if cleaned.endswith("USD"):
        return -sign
    return 0


def _audit_track(calls: Sequence[tuple[str, object, int]]) -> dict[str, object]:
    """1トラック(recommended/analysis)ぶんのスタンス集計。

    calls は (symbol, direction, conviction) の並び。conviction はゲート後に
    0へ落ちている場合があるため、確信度加重は max(conviction, 1) で件数を保証する。
    """
    stances: dict[str, dict[str, int]] = {}
    aggregate = 0
    for symbol, direction, conviction in calls:
        stance = usd_stance(symbol, direction)
        if stance == 0:
            continue
        weight = max(int(conviction), 1)
        stances[str(symbol)] = {"stance": stance, "conviction": int(conviction)}
        aggregate += stance * weight
    signs = {info["stance"] for info in stances.values()}
    contradiction = 1 in signs and -1 in signs
    would_dampen: list[str] = []
    if contradiction and aggregate != 0:
        majority = 1 if aggregate > 0 else -1
        would_dampen = sorted(
            symbol for symbol, info in stances.items() if info["stance"] != majority
        )
    return {
        "stances": stances,
        "contradiction": contradiction,
        "aggregate_score": aggregate,
        # 多数派と逆のUSD観に賭けた側(有効化時の減衰候補)。同点は提案なし
        "would_dampen": would_dampen,
    }


def audit_usd_coherence(entries: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """同一実行の判断群を監査する。

    entries の各要素は symbol / direction / conviction /
    analysis_direction / analysis_conviction を持つMapping(TradePlanから
    呼び出し側が写像する)。recommended はゲート適用後の推奨、analysis は
    ゲート前の分析ビュー。ガードで全推奨がneutral化されている期間も、
    analysis 側で矛盾の観測を継続できる。
    """
    recommended = _audit_track(
        [
            (
                str(entry.get("symbol", "")),
                entry.get("direction"),
                _int(entry.get("conviction")),
            )
            for entry in entries
        ]
    )
    analysis = _audit_track(
        [
            (
                str(entry.get("symbol", "")),
                entry.get("analysis_direction"),
                _int(entry.get("analysis_conviction")),
            )
            for entry in entries
        ]
    )
    return {
        "policy_version": POLICY_VERSION,
        "recommended": recommended,
        "analysis": analysis,
    }


def plan_trace(report: Mapping[str, object], symbol: str) -> dict[str, object] | None:
    """symbolがどちらかのトラックでスタンスを持つ場合、gate_trace用の観測行を返す。

    status は常に observed(ブロックではない)。journal.blocked_gate_names は
    status=blocked のみ拾うため、期待値ガード反実仮想の適格判定には影響しない。
    """
    key = str(symbol)
    rows: dict[str, object] = {}
    for track in ("recommended", "analysis"):
        data = report.get(track)
        if not isinstance(data, Mapping):
            continue
        stances = data.get("stances")
        if isinstance(stances, Mapping) and key in stances:
            rows[track] = {
                "stance": stances[key]["stance"],
                "contradiction": bool(data.get("contradiction")),
                "would_dampen": key in (data.get("would_dampen") or []),
            }
    if not rows:
        return None
    return {
        "gate": GATE_NAME,
        "status": "observed",
        "policy_version": POLICY_VERSION,
        "stage": "shadow",
        "applied": False,
        "dampen_factor_proposal": DAMPEN_FACTOR_PROPOSAL,
        **rows,
    }


def format_warning_ja(report: Mapping[str, object]) -> str:
    """矛盾があった場合の1行警告。無ければ空文字。"""
    parts: list[str] = []
    labels = {"recommended": "推奨", "analysis": "分析(ゲート前)"}
    for track, label in labels.items():
        data = report.get(track)
        if not isinstance(data, Mapping) or not data.get("contradiction"):
            continue
        stances = data.get("stances")
        stance_text = ""
        if isinstance(stances, Mapping):
            stance_text = " ".join(
                f"{symbol}={'USD強' if info['stance'] > 0 else 'USD弱'}"
                for symbol, info in sorted(stances.items())
            )
        dampen = data.get("would_dampen") or []
        note = f"{label}: {stance_text}"
        if dampen:
            note += f" → 少数派{'/'.join(dampen)}が減衰候補(観測のみ・未適用)"
        parts.append(note)
    if not parts:
        return ""
    return "🧭 USD観の内部矛盾を検出 — " + " / ".join(parts)


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
