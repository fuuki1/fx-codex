#!/usr/bin/env python3
"""運用ジャーナルで学習priorの前提を検証し、prior有無の差を測る。

`config/learning_prior_v1.json` は履歴コーパス(HistData 2024年1時間足)で測った
セル間分散の上限である。それを運用判断へ適用してよいかは、次の2点で決まる。

1. **転移するか** — 履歴は「全バー両方向」の無条件母集団、運用は「システムが
   その方向を選んだ」条件付き母集団である。同じ推定量を運用ジャーナルへ当てて、
   実測のセル間分散が履歴の上限を超えていないかを確かめる。
2. **効いているか** — prior有無で減衰セルと確信度係数がどう変わるかを、
   ジャーナルに実際に記録された特徴量分布の上で測る。

副産物として、履歴再現できない特徴量(rating_4h / rating_1d / tf_agreement /
news_count)の tau も運用ジャーナルからは測れる。`--emit-artifact` はそれを
既存artifactへ足した**提案版**を書き出す。自動では採用しない: 人間がdiffを
レビューしてcommitすることで、挙動を変える入力が必ずPRを通る性質を保つ。

    .venv/bin/python tools/learning_prior_audit.py                    # 監査して表示
    .venv/bin/python tools/learning_prior_audit.py --json report.json
    .venv/bin/python tools/learning_prior_audit.py --emit-artifact config/proposed.json

終了コード:
    0 … 監査完了。履歴の上限を超える実測は無い(または判定不能)。
    3 … 実測のセル間分散が履歴の上限を超えた特徴量がある(artifact見直しが必要)。
    1 … 引数不正・入出力エラー。

読み取り専用(`--json` / `--emit-artifact` で指定したpathにだけ書く)。
runtime dataとlaunchdには触れない。追加依存なし・ネットワーク無し。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, UTC
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel import journal, learning, learning_prior, tf_learning  # noqa: E402
from fx_intel.learning_prior import TIMEFRAME_KEY_PREFIX  # noqa: E402
from fx_intel.learning import (  # noqa: E402
    DERIVE_THIN_GAP_HOURS,
    MIN_CONDITION_SAMPLES,
    LearnedProfile,
)
from fx_intel.timeframe import DEFAULT_TIMEFRAMES, PRIMARY_HORIZON_HOURS  # noqa: E402

DEFAULT_PRIOR = REPO_ROOT / "config" / "learning_prior_v1.json"
DEFAULT_FUSION_JOURNAL = REPO_ROOT / "logs" / "briefing_journal.jsonl"
DEFAULT_TF_JOURNAL = REPO_ROOT / "logs" / "briefing_tf_journal.jsonl"
DEFAULT_TF_PRICES = REPO_ROOT / "logs" / "briefing_tf_prices.jsonl"
FUSION_HORIZON_HOURS = journal.DEFAULT_HORIZON_HOURS
# tau をDL推定するのに最低限必要なセル数(3バケット×2方向=6が満杯)。
MIN_CELLS_FOR_TAU = 4
EXIT_TAU_EXCEEDED = 3
VERDICT_CONFIRMS = "confirms"  # 実測 <= 履歴上限。転移を支持する
VERDICT_EXCEEDS = "exceeds"  # 実測の点推定が履歴上限を超えた。artifact見直し
VERDICT_INCONCLUSIVE = "inconclusive"  # 標本不足で判定できない
VERDICT_LIVE_ONLY = "live_only"  # 履歴に無い特徴量(運用実測のみ)


def overlap_factor(horizon_hours: float, thin_gap_hours: float = DERIVE_THIN_GAP_HOURS) -> float:
    """重複ラベル係数。間引き後もホライズンが間引き幅より長ければ標本は重なる。"""
    if thin_gap_hours <= 0:
        return 1.0
    return max(1.0, horizon_hours / thin_gap_hours)


def bucketize(feature: str, value: float) -> str | None:
    """learning.FEATURE_SPECS のバケット名を返す(共有ルーチンへ渡す判定器)。"""
    spec = next((item for item in learning.FEATURE_SPECS if item.key == feature), None)
    if spec is None:
        return None
    bucket = learning.bucket_for(spec, value)
    return bucket.label_ja if bucket is not None else None


def measure_live_tau(
    calls: Sequence[learning.EvaluatedCall], overlap: float
) -> dict[str, dict[str, object]]:
    """採点済み判断から特徴量別に tau を推定する。

    履歴コーパス側(tools/build_learning_prior.py)と同一の
    learning_prior.cell_deviations を使い、(通貨ペア×方向)でde-meanしてから測る。
    de-meanしないと、その期間の相場方向がセルの優位に化けて偽の "exceeds" が出る。
    間引きは derive_profile と同じ幅で行い、実効nは重複係数で割る。
    """
    thinned = learning.thin_calls(list(calls), DERIVE_THIN_GAP_HOURS)
    observations = [
        (call.symbol, call.direction, call.features, call.outcome == "hit")
        for call in thinned
        if call.outcome in ("hit", "miss") and call.direction in ("long", "short")
    ]
    features = [spec.key for spec in learning.FEATURE_SPECS]
    measured: dict[str, dict[str, object]] = {}
    for feature, cells in learning_prior.cell_deviations(
        observations, overlap, features, bucketize
    ).items():
        usable = sum(
            1 for values in cells.detail.values() if values["n_raw"] >= MIN_CONDITION_SAMPLES
        )
        if len(cells.deviations) < MIN_CELLS_FOR_TAU or usable == 0:
            measured[feature] = {
                "cells": len(cells.deviations),
                "usable_cells": usable,
                "tau_hat": None,
            }
            continue
        tau_hat, tau_upper, q_statistic = learning_prior.dersimonian_laird(
            cells.deviations, cells.variances
        )
        effective = sorted(cells.effective)
        measured[feature] = {
            "cells": len(cells.deviations),
            "usable_cells": usable,
            "tau_hat": round(tau_hat, 6),
            "tau_upper95": round(tau_upper, 6),
            "Q": round(q_statistic, 4),
            "n_eff_median": round(effective[len(effective) // 2], 1),
            "max_abs_deviation": round(max(abs(value) for value in cells.deviations), 6),
        }
    return measured


def compare_adjustments(
    baseline: LearnedProfile, shrunk: LearnedProfile, calls: Sequence[learning.EvaluatedCall]
) -> dict[str, object]:
    """ジャーナルの実特徴量分布の上で、prior有無の確信度係数を突き合わせる。"""
    probes = 0
    decayed_before = decayed_after = 0
    factor_before = factor_after = 0.0
    changed = 0
    for call in calls:
        if call.direction not in ("long", "short") or not call.features:
            continue
        before, _reason = baseline.condition_adjustment(call.features, call.direction)
        after, _reason = shrunk.condition_adjustment(call.features, call.direction)
        probes += 1
        decayed_before += before < 1.0
        decayed_after += after < 1.0
        factor_before += before
        factor_after += after
        changed += abs(before - after) > 1e-9
    if probes == 0:
        return {"probes": 0}
    return {
        "probes": probes,
        "decayed_share_before": round(decayed_before / probes, 4),
        "decayed_share_after": round(decayed_after / probes, 4),
        "mean_factor_before": round(factor_before / probes, 4),
        "mean_factor_after": round(factor_after / probes, 4),
        "changed_share": round(changed / probes, 4),
    }


def _fired_cells(profile: LearnedProfile) -> int:
    return sum(
        len(directions)
        for buckets in profile.condition_factors.values()
        for directions in buckets.values()
    )


def _verdict(live: Mapping[str, object], artifact_tau: float | None) -> str:
    if live.get("tau_hat") is None:
        return VERDICT_INCONCLUSIVE
    if artifact_tau is None:
        return VERDICT_LIVE_ONLY
    return VERDICT_EXCEEDS if float(live["tau_hat"]) > artifact_tau else VERDICT_CONFIRMS


def audit_slice(
    slice_key: str,
    calls: Sequence[learning.EvaluatedCall],
    horizon_hours: float,
    prior: learning_prior.LearningPrior | None,
    horizon_label: str,
) -> dict[str, object]:
    baseline = learning.derive_profile(calls, horizon_label=horizon_label)
    shrunk = (
        learning.derive_profile(
            calls, horizon_label=horizon_label, prior=prior, prior_slice=slice_key
        )
        if prior is not None
        else baseline
    )
    overlap = overlap_factor(horizon_hours)
    live = measure_live_tau(calls, overlap)
    features: dict[str, object] = {}
    for feature, measurement in live.items():
        entry = prior.feature(slice_key, feature) if prior is not None else None
        artifact_tau = entry.tau if entry is not None else None
        features[feature] = {
            **measurement,
            "artifact_tau": artifact_tau,
            "artifact_source": entry.source if entry is not None else None,
            "verdict": _verdict(measurement, artifact_tau),
        }
    return {
        "slice": slice_key,
        "horizon_hours": horizon_hours,
        "overlap_factor": overlap,
        "scored": baseline.evaluated,
        "hit_rate": baseline.hit_rate,
        "fired_cells_before": _fired_cells(baseline),
        "fired_cells_after": _fired_cells(shrunk),
        "features": features,
        "adjustment_comparison": compare_adjustments(baseline, shrunk, calls),
    }


def _journal_diagnostics(path: Path, pit_only: bool) -> dict[str, object]:
    """ジャーナル1本の素性。採点0件の原因を「無い/不適格/未成熟」へ切り分ける。"""
    if not path.exists():
        return {"path": str(path), "exists": False}
    entries = list(journal.read_entries(path))
    eligible = [entry for entry in entries if journal.is_pit_eligible_entry(entry)]
    stamps = sorted(str(entry.get("ts") or "") for entry in entries if entry.get("ts"))
    directional = sum(1 for entry in entries if entry.get("direction") in ("long", "short"))
    return {
        "path": str(path),
        "exists": True,
        "rows": len(entries),
        "directional_rows": directional,
        "pit_eligible_rows": len(eligible) if pit_only else None,
        "first_ts": stamps[0] if stamps else None,
        "last_ts": stamps[-1] if stamps else None,
    }


def explain_empty(diagnostics: Mapping[str, object], scored: int) -> str | None:
    """採点0件のときだけ、次に何をすればよいかを1行で返す。"""
    if scored > 0:
        return None
    if not diagnostics.get("exists"):
        return f"{diagnostics['path']} が存在しない(このチェックアウトに runtime data が無い)"
    rows = int(diagnostics.get("rows") or 0)
    if rows == 0:
        return f"{diagnostics['path']} が空"
    if not diagnostics.get("directional_rows"):
        return f"{diagnostics['path']} に方向判断の行が無い(価格行のみ)"
    eligible = diagnostics.get("pit_eligible_rows")
    if eligible is not None and int(eligible) == 0:
        return (
            f"{diagnostics['path']} の{rows}行はすべてPIT不適格"
            "(producer/decision_id/source_record_ids/時刻順序のいずれかが契約を満たさない)"
        )
    return (
        f"{diagnostics['path']} に{rows}行あるが、成熟した判断が0件"
        "(ホライズン先の価格がまだジャーナルに無い)"
    )


def collect_slices(
    fusion_journal: Path, tf_journal: Path, tf_prices: Path
) -> tuple[list[tuple[str, list[learning.EvaluatedCall], float, str]], dict[str, object]]:
    """スライスごとの採点済み判断を、本番と同じ経路で組み立てる。

    返り値の2つ目は診断。採点0件のときに「ファイルが無い」「PIT不適格」
    「未成熟」のどれなのかを呼び出し側が説明できるようにする。
    """
    out: list[tuple[str, list[learning.EvaluatedCall], float, str]] = []
    diagnostics: dict[str, object] = {
        "fusion_journal": _journal_diagnostics(fusion_journal, pit_only=True),
        "tf_journal": _journal_diagnostics(tf_journal, pit_only=True),
        "tf_prices": _journal_diagnostics(tf_prices, pit_only=False),
    }
    if fusion_journal.exists():
        entries = list(journal.read_entries(fusion_journal))
        calls = learning.evaluate_history(
            entries, require_pit=True, include_guard_counterfactuals=True
        )
        out.append((learning_prior.FUSION_KEY, calls, FUSION_HORIZON_HOURS, "約24時間後"))
    if tf_journal.exists():
        entries = [
            entry
            for entry in journal.read_entries(tf_journal)
            if journal.is_pit_eligible_entry(entry)
        ]
        if tf_prices.exists():
            entries = entries + list(journal.read_entries(tf_prices))
        scored_by_timeframe: dict[str, int] = {}
        for timeframe in DEFAULT_TIMEFRAMES:
            calls = tf_learning.evaluate_timeframe_history(entries, timeframe)
            scored_by_timeframe[timeframe] = len(calls)
            if not calls:
                continue
            horizon = PRIMARY_HORIZON_HOURS.get(timeframe, 24.0)
            out.append(
                (
                    learning_prior.timeframe_key(timeframe),
                    calls,
                    horizon,
                    f"約{horizon:g}時間後",
                )
            )
        diagnostics["tf_scored_by_timeframe"] = scored_by_timeframe
    return out, diagnostics


def propose_artifact(
    existing: Mapping[str, object], report: Mapping[str, object]
) -> dict[str, object]:
    """履歴で測れなかった特徴量へ、運用実測の tau を足した提案版を作る。

    キャップは「有意な異質性が無い(点推定 tau_hat = 0)」場合にだけ効かせる。
    その場合の上限は標本誤差だけで決まっており、標本の少ないスライスでは
    実測より広い可動域を与えてしまうため、履歴で測った最大upperで抑える。

    逆に tau_hat > 0 のときはセル間に統計的に支持される差があるので、
    キャップせず実測の上限をそのまま使う。ここでキャップすると、本物の差を
    履歴の水準まで押し潰して調整を殺してしまう。
    """
    proposed = json.loads(json.dumps(existing))  # deep copy
    slices = proposed.setdefault("slices", {})
    added: list[str] = []
    for slice_report in report.get("slices", []):
        slice_key = str(slice_report["slice"])
        bucket = slices.setdefault(slice_key, {})
        historical = [
            float(entry["tau_upper95"])
            for entry in bucket.values()
            if isinstance(entry, Mapping)
            and entry.get("source", learning_prior.SOURCE_HISTORICAL)
            == learning_prior.SOURCE_HISTORICAL
            and isinstance(entry.get("tau_upper95"), (int, float))
        ]
        cap = max(historical) if historical else None
        for feature, measurement in sorted(slice_report["features"].items()):
            if feature in bucket:
                continue  # 履歴で測れているものは上書きしない
            if measurement.get("tau_hat") is None:
                continue
            tau = float(measurement["tau_upper95"])
            significant = float(measurement["tau_hat"]) > 0.0
            capped = tau if significant or cap is None else min(tau, cap)
            bucket[feature] = {
                "tau_upper95": round(capped, 6),
                "tau_hat": measurement["tau_hat"],
                "cells": measurement["cells"],
                "Q": measurement["Q"],
                "n_eff_median": measurement["n_eff_median"],
                "parity": learning_prior.PARITY_UNVERIFIED,
                "source": learning_prior.SOURCE_LIVE_JOURNAL,
                "measured_tau_upper95": round(tau, 6),
                "capped_by_historical_max": None if significant else cap,
                "advisory_only": ["measured_tau_upper95", "capped_by_historical_max"],
            }
            added.append(f"{slice_key}/{feature}")
    proposed["live_audit"] = {
        "generated_at": report.get("generated_at"),
        "added": added,
        "note": (
            "運用ジャーナル実測。履歴再現できない特徴量のみを追加し、"
            "履歴で測った最大upperでキャップした。人間のレビューを経てcommitすること。"
        ),
    }
    return proposed


def render_text(report: Mapping[str, object]) -> str:
    lines: list[str] = []
    for slice_report in report.get("slices", []):
        lines.append(
            f"[{slice_report['slice']}] 採点{slice_report['scored']}件 "
            f"重複係数x{slice_report['overlap_factor']:g} "
            f"減衰セル {slice_report['fired_cells_before']}→{slice_report['fired_cells_after']}"
        )
        comparison = slice_report["adjustment_comparison"]
        if comparison.get("probes"):
            lines.append(
                f"  判断が減衰される割合 {comparison['decayed_share_before']:.1%}"
                f" → {comparison['decayed_share_after']:.1%}"
                f" (係数平均 {comparison['mean_factor_before']:.3f}"
                f" → {comparison['mean_factor_after']:.3f}, "
                f"{comparison['probes']}件で評価)"
            )
        for feature, measurement in sorted(slice_report["features"].items()):
            if measurement.get("tau_hat") is None:
                lines.append(
                    f"  {feature:<14} 判定不能(セル{measurement['cells']}件/"
                    f"閾値到達{measurement['usable_cells']}件)"
                )
                continue
            artifact_tau = measurement["artifact_tau"]
            shown = "なし" if artifact_tau is None else f"{artifact_tau * 100:.2f}%"
            lines.append(
                f"  {feature:<14} 実測tau {float(measurement['tau_hat']) * 100:.2f}%"
                f" (上限{float(measurement['tau_upper95']) * 100:.2f}%)"
                f" / artifact {shown} → {measurement['verdict']}"
            )
    diagnostics = report.get("journals")
    if isinstance(diagnostics, Mapping):
        fusion = diagnostics.get("fusion_journal")
        tf_journal = diagnostics.get("tf_journal")
        scored = {entry["slice"]: entry["scored"] for entry in report.get("slices", [])}
        notes: list[str] = []
        if isinstance(fusion, Mapping):
            note = explain_empty(fusion, int(scored.get(learning_prior.FUSION_KEY, 0)))
            if note:
                notes.append(f"融合: {note}")
        if isinstance(tf_journal, Mapping):
            tf_scored = sum(
                value for key, value in scored.items() if key.startswith(TIMEFRAME_KEY_PREFIX)
            )
            note = explain_empty(tf_journal, tf_scored)
            if note:
                notes.append(f"時間足別: {note}")
        if notes:
            lines.append("")
            lines.extend(notes)
            lines.append("この監査は runtime data(logs/)がある実機で実行する必要があります。")
    if not lines:
        lines.append("ジャーナルが見つからないか、採点済み判断がありません。")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior", type=Path, default=DEFAULT_PRIOR)
    parser.add_argument("--fusion-journal", type=Path, default=DEFAULT_FUSION_JOURNAL)
    parser.add_argument("--tf-journal", type=Path, default=DEFAULT_TF_JOURNAL)
    parser.add_argument("--tf-prices", type=Path, default=DEFAULT_TF_PRICES)
    parser.add_argument("--json", type=Path, default=None, help="レポートをJSONで書き出す")
    parser.add_argument(
        "--emit-artifact", type=Path, default=None, help="運用実測を足した提案artifactを書き出す"
    )
    args = parser.parse_args(argv)

    prior = learning_prior.load_prior(args.prior)
    report: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "prior_path": str(args.prior),
        "prior_loaded": prior is not None,
        "slices": [],
    }
    collected, journals = collect_slices(args.fusion_journal, args.tf_journal, args.tf_prices)
    report["journals"] = journals
    report["slices"] = [
        audit_slice(slice_key, calls, horizon, prior, label)
        for slice_key, calls, horizon, label in collected
    ]
    exceeded = [
        f"{slice_report['slice']}/{feature}"
        for slice_report in report["slices"]
        for feature, measurement in slice_report["features"].items()
        if measurement["verdict"] == VERDICT_EXCEEDS
    ]
    report["tau_exceeded"] = exceeded
    print(render_text(report))
    if exceeded:
        print(f"⚠️ 履歴上限を超えた実測: {', '.join(exceeded)}")

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    if args.emit_artifact is not None:
        if prior is None:
            print("prior artifact を読めないため提案版を作れません", file=sys.stderr)
            return 1
        existing = json.loads(args.prior.read_text(encoding="utf-8"))
        proposed = propose_artifact(existing, report)
        args.emit_artifact.parent.mkdir(parents=True, exist_ok=True)
        args.emit_artifact.write_text(
            json.dumps(proposed, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        added = proposed["live_audit"]["added"]
        print(f"wrote {args.emit_artifact} (追加 {len(added)}件: {', '.join(added) or 'なし'})")
    return EXIT_TAU_EXCEEDED if exceeded else 0


if __name__ == "__main__":
    raise SystemExit(main())
