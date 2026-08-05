#!/usr/bin/env python3
"""正準損益(net-R)契約の読み取り専用監査。

**このツールは一切の書き込みを行わない。** source data を変更せず、cursor を
進めず、キャッシュも append-only evidence も触らない。読むのは入力ファイルだけで、
出力は stdout と `--output` で明示された新規JSONのみ。

監査内容:

1. R空間・JPY空間の恒等式を実データで検証し、不一致件数と理由を出す
2. label_version / provenance / cost_model_id 別の coverage
3. 各損益フィールドの分布(非null・有限・ゼロ・正負・平均・中央値・p10/p90)
4. measured / modelled / simulated / unavailable の分離

⚠️ 恒等式は `fx_intel.evaluation_labels.canonical_net_label_contract_flags` を
**唯一の正**として使う。ここで数式を再実装すると契約が二重定義になるため、
監査側は「契約関数が何をflagしたか」を数えるだけにする。
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.evaluation_labels import (  # noqa: E402
    FULL_COST_NET_LABEL_VERSION,
    KNOWN_EXECUTABLE_COST_MODEL_IDS,
    KNOWN_MEASURED_NONEXECUTABLE_COST_MODEL_IDS,
    NET_LABEL_VERSION,
    PROMOTION_NET_LABEL_VERSIONS,
    canonical_net_label_contract_flags,
)
from fx_intel.provenance import code_hash, config_hash  # noqa: E402

AUDIT_CONTRACT = "pnl-contract-audit-v1"

# 監査対象の損益フィールド。R空間とJPY空間を明示的に分けて扱う。
R_FIELDS: tuple[str, ...] = (
    "gross_realized_r",
    "quote_realized_r",
    "realized_spread_cost_r",
    "entry_spread_r",
    "slippage_r",
    "commission_r",
    "financing_r",
    "conversion_r",
    "additional_cost_r",
    "execution_cost_r",
    "realized_net_r",
    "planned_payoff_r",
)
JPY_FIELDS: tuple[str, ...] = (
    "gross_market_pnl_jpy",
    "executable_pnl_jpy",
    "spread_quote_cost_jpy",
    "slippage_cost_jpy",
    "commission_jpy",
    "financing_jpy",
    "conversion_cost_jpy",
    "net_pnl_jpy",
    "planned_risk_jpy",
)
SCALAR_FIELDS: tuple[str, ...] = ("units",)

# coverage を切る軸。存在しない軸は "<absent>" として数える(ゼロ埋めしない)。
DIMENSIONS: tuple[str, ...] = (
    "label_version",
    "label_provenance",
    "cost_model_id",
    "cost_status",
    "entry_quote_source",
    "exit_quote_source",
    "execution_observation_mode",
    "symbol",
    "timeframe",
    "direction",
    "session",
    "first_touch",
    "net_label_eligible",
    "promotion_eligible",
    "research_only",
    "costs_complete",
)


def _finite(value: object) -> float | None:
    """有限な数値だけを返す。bool は数値として扱わない(True==1 の混入を防ぐ)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _distribution(values: Sequence[float]) -> dict[str, object]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * fraction
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {
        "count": len(ordered),
        "zero": sum(1 for value in ordered if value == 0.0),
        "positive": sum(1 for value in ordered if value > 0.0),
        "negative": sum(1 for value in ordered if value < 0.0),
        "mean": round(statistics.fmean(ordered), 8),
        "median": round(statistics.median(ordered), 8),
        "p10": round(percentile(0.10), 8),
        "p90": round(percentile(0.90), 8),
        "min": round(ordered[0], 8),
        "max": round(ordered[-1], 8),
    }


def _field_summary(records: Sequence[Mapping[str, object]], field: str) -> dict[str, object]:
    present = 0
    finite_values: list[float] = []
    nonfinite = 0
    for record in records:
        if field not in record:
            continue
        raw = record[field]
        if raw is None:
            continue
        present += 1
        number = _finite(raw)
        if number is None:
            nonfinite += 1
        else:
            finite_values.append(number)
    return {
        "total_records": len(records),
        "non_null": present,
        "finite": len(finite_values),
        # 非有限(NaN/inf/非数値)はゼロ埋めせず、そのまま件数で残す
        "non_finite_or_non_numeric": nonfinite,
        "coverage_rate": round(present / len(records), 6) if records else None,
        "distribution": _distribution(finite_values),
    }


def _observation_type(record: Mapping[str, object]) -> str:
    """measured / modelled / simulated / unavailable を分離する。

    ⚠️ 「zero slippage」は実測ゼロではなくモデル仮定なので modelled 側に置く。
    ⚠️ Dukascopy は実測spreadを持つが約定可能性を主張しないので measured_not_executable。
    """
    model_id = str(record.get("cost_model_id") or "")
    provenance = str(record.get("label_provenance") or "")
    if not model_id or model_id == "missing":
        return "unavailable"
    if model_id in KNOWN_MEASURED_NONEXECUTABLE_COST_MODEL_IDS:
        return "measured_not_executable"
    if provenance == "virtual_portfolio_simulated_fill":
        return "simulated"
    if model_id in KNOWN_EXECUTABLE_COST_MODEL_IDS:
        return "modelled_executable_quote"
    return "other"


def _read_jsonl(
    path: Path, limit: int | None, record_path: str = ""
) -> tuple[list[dict], dict[str, object]]:
    """JSONLを読む。corrupt行は握りつぶさず件数として残す。

    record_path は損益フィールドが入れ子になっている場合の取り出し位置
    (例: canonical_outcomes.jsonl は "outcome" の下にある)。
    指定した経路が無い行は unwrap_missing として数え、ゼロ埋めしない。
    """
    rows: list[dict] = []
    malformed = 0
    unwrap_missing = 0
    total_lines = 0
    digest = hashlib.sha256()
    keys = [part for part in record_path.split(".") if part]
    with path.open("rb") as handle:
        for raw in handle:
            digest.update(raw)
            total_lines += 1
            text = raw.strip()
            if not text.startswith(b"{"):
                continue
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(parsed, dict):
                continue
            target: object = parsed
            for key in keys:
                target = target.get(key) if isinstance(target, dict) else None
            if not isinstance(target, dict):
                unwrap_missing += 1
                continue
            rows.append(target)
    if limit is not None and limit > 0:
        rows = rows[-limit:]
    return rows, {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "bytes": path.stat().st_size,
        "total_lines": total_lines,
        "record_path": record_path or "<root>",
        "parsed_objects": len(rows),
        "malformed_lines": malformed,
        "record_path_missing": unwrap_missing,
    }


def _identity_audit(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """契約関数だけを正として恒等式違反を数える。"""
    flag_counts: Counter[str] = Counter()
    checked = 0
    clean = 0
    skipped_no_label = 0
    for record in records:
        if not str(record.get("label_version") or ""):
            skipped_no_label += 1
            continue
        checked += 1
        flags = canonical_net_label_contract_flags(record)
        if flags:
            flag_counts.update(flags)
        else:
            clean += 1
    return {
        "records_with_label_version": checked,
        "records_without_label_version": skipped_no_label,
        "contract_clean": clean,
        "contract_flagged": checked - clean,
        "flag_counts": dict(flag_counts.most_common()),
    }


def _dimension_coverage(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    coverage: dict[str, object] = {}
    for dimension in DIMENSIONS:
        counter: Counter[str] = Counter()
        for record in records:
            if dimension not in record:
                counter["<absent>"] += 1
            else:
                counter[str(record[dimension])] += 1
        coverage[dimension] = dict(counter.most_common(24))
    observation: Counter[str] = Counter()
    for record in records:
        observation[_observation_type(record)] += 1
    coverage["observation_type"] = dict(observation.most_common())
    return coverage


def audit(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    per_field: dict[str, object] = {}
    for field in (*R_FIELDS, *JPY_FIELDS, *SCALAR_FIELDS):
        per_field[field] = _field_summary(records, field)
    label_versions: dict[str, int] = defaultdict(int)
    for record in records:
        label_versions[str(record.get("label_version") or "<none>")] += 1
    return {
        "contract": AUDIT_CONTRACT,
        "read_only": True,
        "code_hash": code_hash(),
        "config_hash": config_hash(),
        "known_label_versions": {
            "promotion_eligible": sorted(PROMOTION_NET_LABEL_VERSIONS),
            "net_r_v2": NET_LABEL_VERSION,
            "net_r_v3_full_cost": FULL_COST_NET_LABEL_VERSION,
        },
        "label_version_counts": dict(label_versions),
        "identity_audit": _identity_audit(records),
        "dimension_coverage": _dimension_coverage(records),
        "field_summary": per_field,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="JSONL files to audit (read-only)")
    parser.add_argument("--limit", type=int, default=0, help="tail N records per file (0=all)")
    parser.add_argument(
        "--record-path",
        default="",
        help='dotted path to the pnl record inside each line (e.g. "outcome")',
    )
    parser.add_argument("--output", type=Path, help="write machine-readable JSON here")
    args = parser.parse_args(list(argv) if argv is not None else None)

    records: list[dict] = []
    sources: list[dict[str, object]] = []
    for path in args.inputs:
        if not path.is_file():
            print(f"input not found: {path}", file=sys.stderr)
            return 2
        rows, meta = _read_jsonl(path, args.limit or None, args.record_path)
        records.extend(rows)
        sources.append(meta)

    report = audit(records)
    report["sources"] = sources
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        # 新規の出力先のみ。source data は決して上書きしない。
        if args.output.exists():
            print(f"refusing to overwrite existing output: {args.output}", file=sys.stderr)
            return 2
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
