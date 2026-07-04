#!/usr/bin/env python3
"""Read-only web dashboard for fx_intel learning state.

This tool intentionally lives outside fx_intel/trader system code. It serves a
small static UI and exposes a read-only JSON summary of logs/*.json/jsonl.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
REPO_ROOT = APP_DIR.parents[1]
DEFAULT_LOG_DIR = REPO_ROOT / "logs"

JOURNAL_FILE = "briefing_journal.jsonl"
LEARNING_FILE = "briefing_learning.json"
ML_FILE = "ml_model.json"
PROMOTION_FILE = "promotion_state.json"
# 時間足別モード(fx_briefing --per-timeframe)の記録
TF_JOURNAL_FILE = "briefing_tf_journal.jsonl"
TF_LEARNING_FILE = "briefing_tf_learning.json"
# 5分ごとの価格スナップショット(fx_tf_snapshot.py)。短い足の採点窓に入る
# 将来価格を密に供給する価格専用系列。採点の将来価格解決に使う(判断は無い)。
TF_PRICES_FILE = "briefing_tf_prices.jsonl"

# 週末クローズ(金曜21:00 UTC → 日曜22:00 UTC)。fx_intel.market と同じ近似。
# ダッシュボードは fx_intel に依存しない方針なのでここに独立して持つ。
_CLOSE_WEEKDAY = 4  # 金曜
_CLOSE_HOUR_UTC = 21
_WEEKEND_CLOSURE = timedelta(hours=49)


def _closure_start_on_or_before(moment: datetime) -> datetime:
    anchor = moment.replace(hour=_CLOSE_HOUR_UTC, minute=0, second=0, microsecond=0)
    anchor -= timedelta(days=(moment.weekday() - _CLOSE_WEEKDAY) % 7)
    if anchor > moment:
        anchor -= timedelta(days=7)
    return anchor


def _open_hours_between(start: datetime, end: datetime) -> float:
    """start→end の経過から週末クローズ分を除いた市場オープン時間(時間単位)。

    fx_intel.market.open_hours_between と同じロジック。採点の将来価格を
    fx_intel 本体と同じ「市場オープン時間換算」で選ぶために使う(壁時計時間で
    採点すると週末跨ぎで本体の学習的中率とズレるため)。
    """
    if end <= start:
        return 0.0
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    closed = timedelta()
    cursor = _closure_start_on_or_before(end_utc)
    while cursor + _WEEKEND_CLOSURE > start_utc:
        overlap = min(cursor + _WEEKEND_CLOSURE, end_utc) - max(cursor, start_utc)
        if overlap > timedelta():
            closed += overlap
        cursor -= timedelta(days=7)
    return (end_utc - start_utc - closed).total_seconds() / 3600.0


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_journal(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _file_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size": 0, "mtime": None, "path": str(path)}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "path": str(path),
    }


def _future_close(
    series: list[tuple[datetime, float]],
    ts: datetime,
    horizon_hours: float = 24.0,
    tolerance_hours: float = 2.0,
) -> float | None:
    """記録時刻から主ホライズン後(市場オープン時間換算)に最も近い終値。

    fx_intel.price_history.future_close_from_series と同じく、経過は
    _open_hours_between で数える(週末クローズを除外)。壁時計の候補窓で
    ざっくり絞ってから、オープン時間換算の age で厳密に判定する。
    """
    if not series:
        return None
    # オープン時間は壁時計を超えないため、候補は壁時計で
    # [下限, 上限 + 週末クローズ1回分] に限られる
    window_lower = ts + timedelta(hours=horizon_hours - tolerance_hours)
    window_upper = ts + timedelta(hours=horizon_hours + tolerance_hours) + _WEEKEND_CLOSURE
    best: tuple[float, float] | None = None
    for point_ts, close in series:
        if point_ts < window_lower:
            continue
        if point_ts > window_upper:
            break
        age = _open_hours_between(ts, point_ts)
        if not (horizon_hours - tolerance_hours <= age <= horizon_hours + tolerance_hours):
            continue
        gap = abs(age - horizon_hours)
        if best is None or gap < best[0]:
            best = (gap, close)
    return best[1] if best is not None else None


# 時間足別ジャーナルの主ホライズン(時間)と採点許容誤差(±時間)。
# fx_intel.timeframe と同じ値。dashboard は fx_intel に依存しない方針なので
# ここに独立して持つ(ズレたら表示だけの問題で、採点の正は fx_intel 側)。
_HORIZON_TOLERANCE = {
    0.25: 0.1,
    0.5: 0.15,
    1.0: 0.25,
    4.0: 1.0,
    8.0: 1.5,
    12.0: 2.0,
    24.0: 2.0,
    48.0: 4.0,
    72.0: 6.0,
}


def _tolerance_for(horizon_hours: float) -> float:
    return _HORIZON_TOLERANCE.get(horizon_hours, 2.0)


def _evaluate_journal(entries: list[dict[str, Any]]) -> dict[str, Any]:
    # 価格系列は (symbol, timeframe) 別に持つ。timeframe を持たない旧スキーマ行は
    # timeframe="" のキー(融合1判断)に入り、従来どおり24h採点される。
    prices: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        close = _number(entry.get("close"))
        symbol = str(entry.get("symbol") or "")
        timeframe = str(entry.get("timeframe") or "")
        if ts is None:
            continue
        parsed.append((ts, entry))
        if close is not None and symbol:
            prices.setdefault((symbol, timeframe), []).append((ts, close))
    for series in prices.values():
        series.sort(key=lambda row: row[0])

    evaluated = hits = flat = pending = directional = 0
    by_symbol: dict[str, dict[str, int]] = {}
    by_timeframe: dict[str, dict[str, int]] = {}
    outcomes: list[dict[str, Any]] = []
    for ts, entry in parsed:
        direction = str(entry.get("direction") or "")
        if direction not in {"long", "short"}:
            continue
        directional += 1
        symbol = str(entry.get("symbol") or "")
        timeframe = str(entry.get("timeframe") or "")
        close = _number(entry.get("close"))
        atr = _number(entry.get("atr"))
        if close is None or not symbol:
            pending += 1
            continue
        # その足の主ホライズンで採点(旧スキーマ行=24h)
        horizon = _number(entry.get("horizon_hours")) or 24.0
        future = _future_close(
            prices.get((symbol, timeframe), []),
            ts,
            horizon_hours=horizon,
            tolerance_hours=_tolerance_for(horizon),
        )
        if future is None:
            pending += 1
            continue
        move = future - close
        signed = move if direction == "long" else -move
        threshold = (atr or 0.0) * 0.1
        if abs(signed) <= threshold:
            flat += 1
            outcome = "flat"
        else:
            evaluated += 1
            hit = signed > 0
            hits += int(hit)
            stat = by_symbol.setdefault(symbol, {"evaluated": 0, "hits": 0, "flat": 0})
            stat["evaluated"] += 1
            stat["hits"] += int(hit)
            if timeframe:
                tf_stat = by_timeframe.setdefault(timeframe, {"evaluated": 0, "hits": 0, "flat": 0})
                tf_stat["evaluated"] += 1
                tf_stat["hits"] += int(hit)
            outcome = "hit" if hit else "miss"
        outcomes.append(
            {
                "ts": ts.isoformat(),
                "symbol": symbol,
                "timeframe": timeframe,
                "direction": direction,
                "outcome": outcome,
                "move": round(move, 6),
            }
        )
    return {
        "directional": directional,
        "evaluated": evaluated,
        "hits": hits,
        "flat": flat,
        "pending": pending,
        "hit_rate": hits / evaluated if evaluated else None,
        "by_symbol": by_symbol,
        "by_timeframe": by_timeframe,
        "recent_outcomes": outcomes[-20:],
    }


def _journal_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_symbol: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    latest_ts: datetime | None = None
    for entry in entries:
        symbol = str(entry.get("symbol") or "unknown")
        direction = str(entry.get("direction") or "unknown")
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
        by_direction[direction] = by_direction.get(direction, 0) + 1
        ts = _parse_ts(entry.get("ts"))
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
    recent = entries[-30:]
    return {
        "total": len(entries),
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "latest_ts": latest_ts.isoformat() if latest_ts else None,
        "recent": recent,
    }


def _symbol_rows(learning: dict[str, Any], evaluated: dict[str, Any]) -> list[dict[str, Any]]:
    raw_stats = learning.get("symbol_stats")
    if not isinstance(raw_stats, dict):
        raw_stats = evaluated.get("by_symbol", {})
    rows: list[dict[str, Any]] = []
    raw_factors = learning.get("symbol_factors")
    factors: dict[str, Any] = raw_factors if isinstance(raw_factors, dict) else {}
    for symbol, stat in raw_stats.items():
        if not isinstance(stat, dict):
            continue
        n = int(stat.get("evaluated", 0) or 0)
        h = int(stat.get("hits", 0) or 0)
        rows.append(
            {
                "symbol": symbol,
                "evaluated": n,
                "hits": h,
                "hit_rate": h / n if n else None,
                "factor": _number(factors.get(symbol)) or 1.0,
            }
        )
    rows.sort(key=lambda row: (-row["evaluated"], row["symbol"]))
    return rows


def _condition_rows(learning: dict[str, Any]) -> list[dict[str, Any]]:
    stats = learning.get("condition_stats")
    factors = learning.get("condition_factors")
    if not isinstance(stats, dict):
        return []
    if not isinstance(factors, dict):
        factors = {}
    rows: list[dict[str, Any]] = []
    for feature, buckets in stats.items():
        if not isinstance(buckets, dict):
            continue
        for bucket, directions in buckets.items():
            if not isinstance(directions, dict):
                continue
            for direction, cell in directions.items():
                if not isinstance(cell, dict):
                    continue
                n = int(cell.get("evaluated", 0) or 0)
                h = int(cell.get("hits", 0) or 0)
                factor = (
                    factors.get(feature, {}).get(bucket, {}).get(direction)
                    if isinstance(factors.get(feature), dict)
                    else None
                )
                rows.append(
                    {
                        "feature": feature,
                        "bucket": bucket,
                        "direction": direction,
                        "evaluated": n,
                        "hits": h,
                        "hit_rate": h / n if n else None,
                        "factor": _number(factor),
                    }
                )
    rows.sort(
        key=lambda row: (row["hit_rate"] if row["hit_rate"] is not None else 2, -row["evaluated"])
    )
    return rows[:20]


def _ml_summary(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    importance = payload.get("importance_by_name")
    if not isinstance(importance, dict):
        importance = {}
    importance_rows: list[dict[str, Any]] = [
        {"name": str(name), "value": float(value)}
        for name, value in importance.items()
        if isinstance(value, (int, float))
    ]
    importance_rows.sort(key=lambda row: float(row["value"]), reverse=True)
    return {
        "trained_at": payload.get("trained_at"),
        "usable": bool(payload.get("usable", False)),
        "n_train": int(payload.get("n_train", 0) or 0),
        "n_valid": int(payload.get("n_valid", 0) or 0),
        "base_rate": _number(payload.get("base_rate")),
        "metrics": metrics,
        "reasons": payload.get("reasons") if isinstance(payload.get("reasons"), list) else [],
        "importance": importance_rows[:12],
        "has_model": payload.get("model") is not None,
    }


def _promotion_summary(payload: dict[str, Any]) -> dict[str, Any]:
    raw_stages = payload.get("stages")
    stages: dict[str, Any] = raw_stages if isinstance(raw_stages, dict) else {}
    raw_history = payload.get("history")
    history: list[Any] = raw_history if isinstance(raw_history, list) else []
    return {
        "stages": {"macro": stages.get("macro", "shadow"), "ml": stages.get("ml", "shadow")},
        "updated_at": payload.get("updated_at"),
        "history": [row for row in history if isinstance(row, dict)][-20:],
    }


def build_state(log_dir: Path) -> dict[str, Any]:
    journal_path = log_dir / JOURNAL_FILE
    learning_path = log_dir / LEARNING_FILE
    ml_path = log_dir / ML_FILE
    promotion_path = log_dir / PROMOTION_FILE
    tf_journal_path = log_dir / TF_JOURNAL_FILE
    tf_learning_path = log_dir / TF_LEARNING_FILE
    tf_prices_path = log_dir / TF_PRICES_FILE

    entries = _read_journal(journal_path)
    tf_entries = _read_journal(tf_journal_path)
    # 価格スナップショット(direction 無し)。採点対象は増えないが、短い足の
    # 将来価格系列を密にして 15m/1h も採点可能にする(fx_briefing 本体と同じ結合)。
    tf_price_rows = _read_journal(tf_prices_path)
    learning = _load_json(learning_path)
    ml = _load_json(ml_path)
    promotion = _load_json(promotion_path)
    # 融合1判断行(24h)と時間足別行(各主ホライズン)を同じ採点器で評価する。
    # _evaluate_journal は行ごとに timeframe/horizon_hours を見て採点する。
    # 価格スナップショット行は将来価格系列にだけ寄与する(direction 無しなので
    # 採点対象=directional にはカウントされない)。
    evaluated = _evaluate_journal(entries + tf_entries + tf_price_rows)

    evaluated_count = int(learning.get("evaluated", 0) or evaluated["evaluated"])
    hits = int(learning.get("hits", 0) or evaluated["hits"])

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "log_dir": str(log_dir),
        "files": {
            JOURNAL_FILE: _file_status(journal_path),
            LEARNING_FILE: _file_status(learning_path),
            ML_FILE: _file_status(ml_path),
            PROMOTION_FILE: _file_status(promotion_path),
            TF_JOURNAL_FILE: _file_status(tf_journal_path),
            TF_LEARNING_FILE: _file_status(tf_learning_path),
            TF_PRICES_FILE: _file_status(tf_prices_path),
        },
        "journal": _journal_summary(entries + tf_entries),
        "evaluation": evaluated,
        "learning": {
            "generated_at": learning.get("generated_at"),
            "evaluated": evaluated_count,
            "hits": hits,
            "flat": int(learning.get("flat", 0) or evaluated["flat"]),
            "hit_rate": hits / evaluated_count if evaluated_count else None,
            "tech_weight": _number(learning.get("tech_weight")) or 0.55,
            "news_weight": _number(learning.get("news_weight")) or 0.45,
            "tech_hit_rate": _number(learning.get("tech_hit_rate")),
            "news_hit_rate": _number(learning.get("news_hit_rate")),
            "conviction_brier": _number(learning.get("conviction_brier")),
            "conviction_brier_base": _number(learning.get("conviction_brier_base")),
            "bins": learning.get("bins") if isinstance(learning.get("bins"), list) else [],
            "notes_ja": (
                learning.get("notes_ja") if isinstance(learning.get("notes_ja"), list) else []
            ),
            "symbols": _symbol_rows(learning, evaluated),
            "conditions": _condition_rows(learning),
        },
        "ml": _ml_summary(ml),
        "promotion": _promotion_summary(promotion),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "FxLearningDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            raw_log_dir = query.get("logDir", [str(self.server.log_dir)])[0]  # type: ignore[attr-defined]
            log_dir = Path(raw_log_dir).expanduser().resolve()
            self._send_json(build_state(log_dir))
            return
        if parsed.path in {"", "/"}:
            self._send_file(STATIC_DIR / "index.html")
            return
        target = (STATIC_DIR / parsed.path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        self._send_file(target)

    def log_message(self, fmt: str, *args: object) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {fmt % args}")

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or path.suffix in {".js", ".css"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only fx_intel learning dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(os.environ.get("FX_LEARNING_LOG_DIR", DEFAULT_LOG_DIR)),
        help="Directory containing briefing_journal.jsonl / briefing_learning.json / ml_model.json",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.log_dir = args.log_dir.expanduser().resolve()  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/"
    print(f"AI learning dashboard: {url}")
    print(f"Reading logs from: {server.log_dir}")  # type: ignore[attr-defined]
    print("Read-only mode. Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
