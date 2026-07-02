#!/usr/bin/env python3
"""strategy_params.json の安全ゲート（生成側・読み込み側の共有モジュール）。

自動最適化 → rsync → ホットリロードという経路で、検証されていないパラメータが
ライブ戦略に流れ込む事故を防ぐ。ゲートは2か所に置く:

- 生成側 (auto_optimize.py): validate_data_source() で最適化に使うデータ自体を検証。
  同梱の合成サンプル（乱数生成）や、行数・期間が不足するデータでの最適化を拒否する。
- 読み込み側 (Mac mini の strategy.py / promote_params.py): validate_params() /
  load_validated_params() で、来歴（provenance）の無いパラメータ・境界値を外れた
  パラメータを拒否する。拒否時は呼び出し側が現行パラメータを維持すること。

Mac mini へは rsync でこのファイルごと同期される前提のため、依存は標準ライブラリのみ。
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

# 同梱サンプル（examples/generate_sample_data.py が乱数で生成する合成データ）。
# パス比較だけでは別の場所へコピーされた場合に素通りするため、内容ハッシュでも検知する。
_REPO_ROOT = Path(__file__).resolve().parent
BUNDLED_SAMPLE = _REPO_ROOT / "examples" / "sample_prices.csv"
KNOWN_SYNTHETIC_SHA256 = frozenset(
    {
        # examples/sample_prices.csv (rng seed=42, 2026-07 時点)
        "b93513ba74070117edc02f404d285670b13f53772ac4ce91a10c87b6c398e427",
    }
)

# 最適化データの最低要件。合成サンプル（2700行・約37日）はここでも弾かれる（多層防御）。
MIN_DATA_ROWS = 1000
MIN_DATA_SPAN_DAYS = 180

# 配備パラメータの許容範囲。ここを外れる値は最適化のバグか汚染とみなす。
PARAM_BOUNDS = {
    "fast_window": (2, 200),
    "slow_window": (5, 500),
    "atr_window": (2, 100),
    "atr_multiple": (0.5, 10.0),
}

# 来歴に記録された取引数がこれ未満のパラメータは統計的に信頼できないため拒否。
MIN_TRADE_COUNT = 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def synthetic_hashes() -> set[str]:
    """既知の合成データのハッシュ集合。同梱サンプルが再生成されていても検知できるよう、
    定数に加えて現在のサンプルファイルの実ハッシュも含める。"""
    hashes = set(KNOWN_SYNTHETIC_SHA256)
    if BUNDLED_SAMPLE.is_file():
        hashes.add(sha256_file(BUNDLED_SAMPLE))
    return hashes


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        return None


def validate_data_source(
    raw: str | None,
    *,
    min_rows: int = MIN_DATA_ROWS,
    min_span_days: int = MIN_DATA_SPAN_DAYS,
) -> tuple[Path | None, list[str]]:
    """最適化に使う価格CSVを検証する。(path, errors) を返し、errors が空でなければ
    最適化を実行してはならない。"""
    if raw is None or not raw.strip():
        return None, [
            "データが未指定。実データ CSV を --data か OPTIMIZE_DATA で必ず指定すること"
            "（同梱サンプルでの最適化は禁止）"
        ]
    path = Path(raw.strip()).expanduser()
    try:
        resolved = path.resolve()
    except OSError as e:
        return None, [f"データパスを解決できない: {raw!r} ({e})"]
    if not resolved.is_file():
        return None, [f"データファイルが存在しない: {resolved}"]

    errors: list[str] = []
    if BUNDLED_SAMPLE.is_file() and resolved == BUNDLED_SAMPLE.resolve():
        errors.append(
            "同梱サンプル（乱数生成の合成データ）が指定されている。実データを指定すること"
        )
    if sha256_file(resolved) in synthetic_hashes():
        errors.append(
            "内容が同梱サンプル（合成データ）と一致する。コピーであっても最適化には使えない"
        )
    if errors:
        return None, errors

    rows = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    with open(resolved, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return None, [f"CSV が空: {resolved}"]
        columns = [c.strip().lower() for c in header]
        for required in ("timestamp", "close"):
            if required not in columns:
                errors.append(f"CSV に {required} 列がない: {resolved}")
        if errors:
            return None, errors
        ts_idx = columns.index("timestamp")
        for row in reader:
            if not row:
                continue
            rows += 1
            ts = _parse_timestamp(row[ts_idx]) if len(row) > ts_idx else None
            if ts is None:
                continue
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

    if rows < min_rows:
        errors.append(f"データ行数が不足: {rows} 行（最低 {min_rows} 行）")
    if first_ts is None or last_ts is None:
        errors.append("timestamp 列を解釈できない（ISO形式であること）")
    else:
        span_days = (last_ts - first_ts).days
        if span_days < min_span_days:
            errors.append(
                f"データ期間が不足: {span_days} 日（最低 {min_span_days} 日）。"
                "短期間データへの過剰適合を防ぐため拒否"
            )
    if errors:
        return None, errors
    return resolved, []


def data_provenance(path: Path, rows: int, start: str, end: str) -> dict:
    """candidate に埋め込むデータ来歴。読み込み側の validate_params() が必須とする。"""
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "rows": rows,
        "start": start,
        "end": end,
    }


def _check_number(
    params: dict, key: str, lo: float, hi: float, errors: list[str], *, integer: bool
) -> None:
    value = params.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"{key} が数値でない: {value!r}")
        return
    if integer and int(value) != value:
        errors.append(f"{key} が整数でない: {value!r}")
        return
    if not (lo <= value <= hi):
        errors.append(f"{key}={value} が許容範囲 [{lo}, {hi}] を外れている")


def validate_params(
    params: dict,
    *,
    min_trade_count: int = MIN_TRADE_COUNT,
) -> list[str]:
    """配備パラメータを検証し、エラーの一覧を返す（空なら合格）。

    読み込み側（strategy.py / promote_params.py）はエラーが1つでもあれば
    このパラメータを適用せず、現行パラメータを維持すること。"""
    if not isinstance(params, dict):
        return [f"パラメータが dict でない: {type(params).__name__}"]

    errors: list[str] = []
    for key, (lo, hi) in PARAM_BOUNDS.items():
        _check_number(params, key, lo, hi, errors, integer=key.endswith("_window"))

    fast = params.get("fast_window")
    slow = params.get("slow_window")
    if isinstance(fast, (int, float)) and isinstance(slow, (int, float)) and fast >= slow:
        errors.append(f"fast_window={fast} >= slow_window={slow}（クロスが定義できない）")

    provenance = params.get("provenance")
    if not isinstance(provenance, dict):
        errors.append(
            "provenance（来歴メタデータ）が無い。auto_optimize.py の安全ゲートを"
            "通っていないパラメータは配備できない"
        )
        return errors

    data = provenance.get("data")
    if not isinstance(data, dict):
        errors.append("provenance.data（最適化データの来歴）が無い")
    else:
        for key in ("path", "sha256", "rows", "start", "end"):
            if not data.get(key):
                errors.append(f"provenance.data.{key} が無い")
        sha = data.get("sha256")
        if isinstance(sha, str) and sha in synthetic_hashes():
            errors.append("合成サンプルデータで最適化されたパラメータ。配備禁止")

    trade_count = provenance.get("trade_count")
    if not isinstance(trade_count, int) or isinstance(trade_count, bool):
        errors.append(f"provenance.trade_count が整数でない: {trade_count!r}")
    elif trade_count < min_trade_count:
        errors.append(
            f"取引数が不足: {trade_count}（最低 {min_trade_count}）。"
            "統計的に信頼できないパラメータは配備できない"
        )

    updated_at = params.get("updated_at")
    if not isinstance(updated_at, str) or _parse_timestamp(updated_at) is None:
        errors.append(f"updated_at が解釈できない: {updated_at!r}")

    return errors


def load_validated_params(
    path: Path | str,
    *,
    min_trade_count: int = MIN_TRADE_COUNT,
) -> tuple[dict | None, list[str]]:
    """パラメータファイルを読み込んで検証する。(params, errors) を返す。

    例外を投げない読み込み側の入口。strategy.py はここで None が返ったら
    現行パラメータを維持し、errors を Discord 通知に回すこと。"""
    path = Path(path)
    if not path.is_file():
        return None, [f"パラメータファイルが存在しない: {path}"]
    try:
        params = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"パラメータファイルを読めない: {path} ({e})"]
    errors = validate_params(params, min_trade_count=min_trade_count)
    if errors:
        return None, errors
    return params, []
