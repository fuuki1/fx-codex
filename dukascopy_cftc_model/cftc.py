"""CFTC COT(Commitments of Traders)の**時系列**取得。

fx_intel/macro.py は「最新1週の COT スナップショット」だけを保持する
(方向判断用)。本モジュールは Ridge の特徴量として使うため、**全報告週の
時系列**(非商業=投機筋 long/short/net、商業 long/short、建玉)を取得する。

- 契約コードは fx_intel/macro.py の COT_CONTRACT_CODES を再利用(EUR=099741 等)。
- Socrata API は $where/$order/$limit でページングできる。週次データなので
  260週(約5年)でも1回のリクエストに収まる。
- report_date は火曜集計。発表は同週金曜(約3日ラグ)。ラグの扱いは
  as-of 結合を行う features.py 側の責務で、ここでは「集計日」をそのまま返す。

parse_cot_history はネットワーク非依存の純粋関数(JSON配列 → DataFrame)。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import date, datetime, UTC
from pathlib import Path

import pandas as pd
import requests

from fx_intel.macro import COT_CONTRACT_CODES, CFTC_COT_URL

USER_AGENT = "fx-codex-dcm/1.0 (+https://github.com/fuuki1)"
FETCH_ATTEMPTS = 3
FETCH_RETRY_WAIT_SECONDS = 1.5
FETCH_TIMEOUT_SECONDS = 30.0

# 取得する Socrata 列。noncomm=非商業(投機筋)、comm=商業(実需・ヘッジャー)。
COT_SELECT_FIELDS = (
    "report_date_as_yyyy_mm_dd",
    "noncomm_positions_long_all",
    "noncomm_positions_short_all",
    "comm_positions_long_all",
    "comm_positions_short_all",
    "open_interest_all",
)

COT_COLUMNS = [
    "report_date",
    "noncomm_long",
    "noncomm_short",
    "comm_long",
    "comm_short",
    "open_interest",
]


def contract_code(currency: str) -> str:
    """通貨(EUR/JPY/...)→ CFTC契約コード。未対応通貨は KeyError。"""
    code = COT_CONTRACT_CODES.get(currency.upper())
    if code is None:
        raise KeyError(f"COT契約コード未対応の通貨: {currency}(対応: {sorted(COT_CONTRACT_CODES)})")
    return code


def _to_int(value: object) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def parse_cot_history(payload: object) -> pd.DataFrame:
    """Socrata JSON配列 → COT時系列 DataFrame(純粋関数・報告日昇順)。

    列: report_date(date), noncomm_long, noncomm_short, comm_long, comm_short,
        open_interest, net_noncomm(=long-short)。
    数値化できない行はスキップ。report_date 昇順・重複日は最後を採用。
    """
    if not isinstance(payload, list):
        return _empty_cot_frame()
    records: dict[date, dict[str, int]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("report_date_as_yyyy_mm_dd", ""))[:10]
        try:
            report_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        nl = _to_int(row.get("noncomm_positions_long_all"))
        ns = _to_int(row.get("noncomm_positions_short_all"))
        if nl is None or ns is None:
            continue  # 投機筋の long/short が欠けている行は特徴量に使えない(mypy narrowing)
        records[report_date] = {
            "noncomm_long": nl,
            "noncomm_short": ns,
            "comm_long": _to_int(row.get("comm_positions_long_all")) or 0,
            "comm_short": _to_int(row.get("comm_positions_short_all")) or 0,
            "open_interest": _to_int(row.get("open_interest_all")) or 0,
        }
    if not records:
        return _empty_cot_frame()
    ordered = sorted(records.items(), key=lambda kv: kv[0])
    frame = pd.DataFrame(
        [{"report_date": d, **vals} for d, vals in ordered],
    )
    frame["net_noncomm"] = frame["noncomm_long"] - frame["noncomm_short"]
    frame["report_date"] = pd.to_datetime(frame["report_date"])
    return frame.reset_index(drop=True)


def _empty_cot_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=[*COT_COLUMNS, "net_noncomm"])
    frame["report_date"] = pd.to_datetime(frame["report_date"])
    return frame


# ---------------------------------------------------------------- キャッシュ付き取得


def _cot_cache_path(cache_dir: Path, currency: str) -> Path:
    return cache_dir / "cot" / f"{currency.upper()}.json"


def _cot_query_params(code: str, start: date, limit: int) -> dict[str, str | int]:
    return {
        "$select": ",".join(COT_SELECT_FIELDS),
        "$where": (
            f"cftc_contract_market_code='{code}' "
            f"AND report_date_as_yyyy_mm_dd >= '{start.isoformat()}T00:00:00'"
        ),
        "$order": "report_date_as_yyyy_mm_dd ASC",
        "$limit": limit,
    }


def fetch_cot_history(
    currency: str,
    start: date,
    cache_dir: Path,
    cache_ttl_hours: float = 24.0,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """通貨の COT週次時系列を取得(start以降・キャッシュ優先)。

    ネットワーク失敗時は期限切れでもキャッシュを使う(stale許容)。
    キャッシュが全く無く取得も失敗した場合のみ空 DataFrame を返す。
    """
    currency = currency.upper()
    code = contract_code(currency)
    cache_path = _cot_cache_path(cache_dir, currency)

    cached_payload = _load_cache_payload(cache_path)
    if (
        cached_payload is not None
        and not _cache_expired(cache_path, cache_ttl_hours)
        and _cache_covers_start(cached_payload, start)
    ):
        return _slice_from(parse_cot_history(cached_payload["rows"]), start)

    http = session or requests
    params = _cot_query_params(code, start, limit=1000)
    last_exc: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            resp = http.get(
                CFTC_COT_URL,
                params=params,
                timeout=FETCH_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(FETCH_RETRY_WAIT_SECONDS * (attempt + 1))
            continue
        if resp.status_code == 200:
            rows = resp.json()
            _save_cache_payload(cache_path, rows)
            return _slice_from(parse_cot_history(rows), start)
        last_exc = RuntimeError(f"CFTC HTTP {resp.status_code}")
        time.sleep(FETCH_RETRY_WAIT_SECONDS * (attempt + 1))

    # 取得失敗 → 期限切れでもキャッシュがあれば使う
    if cached_payload is not None:
        return _slice_from(parse_cot_history(cached_payload["rows"]), start)
    if last_exc is not None:
        # キャッシュも無く取得も不能。呼び出し側が品質ゲートで扱えるよう空を返す。
        return _empty_cot_frame()
    return _empty_cot_frame()


def _slice_from(frame: pd.DataFrame, start: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    start_ts = pd.Timestamp(start)
    return frame[frame["report_date"] >= start_ts].reset_index(drop=True)


def _cache_covers_start(payload: Mapping, start: date) -> bool:
    """キャッシュが要求開始日をカバーしているか。

    以前の実行で「もっと後の start」で取得したキャッシュが残っていると、
    古い期間の価格に対して COT が全く重ならない(全NaN化)。キャッシュの
    最古行が要求 start より後なら、カバー不足として再取得させる。
    (start より1週ぶんの余裕は許容 — 週次データの端数のため。)
    """
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return False
    earliest: date | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            d = date.fromisoformat(str(row.get("report_date_as_yyyy_mm_dd", ""))[:10])
        except ValueError:
            continue
        if earliest is None or d < earliest:
            earliest = d
    if earliest is None:
        return False
    # 最古行が start より後(=カバー不足)なら False。1週の余裕を持たせる。
    return (earliest - start).days <= 7


def _load_cache_payload(cache_path: Path) -> dict | None:
    import json

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return payload
    return None


def _save_cache_payload(cache_path: Path, rows: object) -> None:
    import json

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"fetched_at": datetime.now(UTC).isoformat(), "rows": rows}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _cache_expired(cache_path: Path, ttl_hours: float) -> bool:
    payload = _load_cache_payload(cache_path)
    if payload is None:
        return True
    try:
        fetched_at = datetime.fromisoformat(str(payload.get("fetched_at", "")))
    except ValueError:
        return True
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    age_hours = (datetime.now(UTC) - fetched_at).total_seconds() / 3600.0
    return age_hours > ttl_hours
