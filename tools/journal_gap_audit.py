"""判断ジャーナルの重複・欠損・時刻逆転を監査する読み取り専用ツール。

2026-07-10のMac mini監査で「手動loop×3 + launchd」の多重起動により
ジャーナルへ毎時2〜3回の重複判断が書き込まれていたことが判明した。
重複は学習(learning.py)のサンプル数を水増しし的中率推定を歪めるため、
どの期間が何倍に汚染され、どの期間が欠損しているかを監査証跡として残す。

方針:
- このツールはジャーナルを一切変更しない(読み取り専用)。
  クリーンアップは人間がレポートを確認した上で別途判断する
- 欠損期間は現在値からの補間・捏造をしない。期間をそのまま記録する
- 週末クローズ(金21:00 UTC〜日22:00 UTC)は書込みが継続する設計だが、
  欠損判定の文脈情報として market_closed フラグを付ける

出力: JSON(標準出力) + 人間向けサマリ(標準エラー)
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, UTC
import json
from pathlib import Path
import sys

# fx_intelと同じ週末近似(market.py)。stdlib単体で動かすためここで再定義はせず
# インポートする(このツールはリポジトリ内での実行を前提とする)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fx_intel.market import is_market_open  # noqa: E402

# 同一(symbol, timeframe)の判断がこの分数以内に複数あれば「多重起動の重複」とみなす。
# 正規の書込みは毎時:10の1回だけなので、30分以内の再出現は正常系では起こらない
DUPLICATE_WINDOW_MINUTES = 30.0


def read_journal(path: Path) -> list[dict]:
    rows: list[dict] = []
    broken = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            broken += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
    if broken:
        print(f"[gap_audit] 壊れた行を{broken}行スキップ", file=sys.stderr)
    return rows


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def audit_journal(
    rows: list[dict],
    expected_interval_hours: float = 1.0,
    duplicate_window_minutes: float = DUPLICATE_WINDOW_MINUTES,
) -> dict:
    """重複・欠損・時刻逆転を集計する。"""
    parsed: list[tuple[datetime, str, str]] = []
    unparsable = 0
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None:
            unparsable += 1
            continue
        parsed.append((ts, str(row.get("symbol", "")), str(row.get("timeframe", "fusion"))))

    # 時刻逆転(追記型JSONLで順序が乱れるのは多重writerの兆候)
    reversals = sum(1 for earlier, later in zip(parsed, parsed[1:]) if later[0] < earlier[0])

    # (symbol, timeframe)別に時系列を組み、重複と欠損を検出
    series: dict[tuple[str, str], list[datetime]] = {}
    for ts, symbol, timeframe in parsed:
        series.setdefault((symbol, timeframe), []).append(ts)

    duplicate_groups = 0
    duplicate_rows = 0
    gaps: list[dict] = []
    gap_threshold = timedelta(hours=expected_interval_hours * 2)
    window = timedelta(minutes=duplicate_window_minutes)
    for (symbol, timeframe), stamps in series.items():
        stamps.sort()
        cluster = 1
        for earlier, later in zip(stamps, stamps[1:]):
            delta = later - earlier
            if delta <= window:
                cluster += 1
            else:
                if cluster > 1:
                    duplicate_groups += 1
                    duplicate_rows += cluster - 1
                cluster = 1
            if delta > gap_threshold:
                gaps.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "gap_start": earlier.isoformat(),
                        "gap_end": later.isoformat(),
                        "gap_hours": round(delta.total_seconds() / 3600, 2),
                        # 週末クローズ中は判断自体はstandbyでも書込みは続く設計。
                        # ただし歴史的にループ停止と休場が重なる場合の文脈として残す
                        "market_closed_at_start": not is_market_open(earlier),
                    }
                )
        if cluster > 1:
            duplicate_groups += 1
            duplicate_rows += cluster - 1

    per_hour = Counter(ts.strftime("%Y-%m-%dT%H") for ts, _, _ in parsed)
    multi_writer_hours = sum(
        1
        for hour, count in per_hour.items()
        if count > len(series)  # 1時間に(symbol×timeframe)数を超える行=多重書込み
    )

    return {
        "total_rows": len(rows),
        "unparsable_ts": unparsable,
        "series_count": len(series),
        "time_reversals": reversals,
        "duplicate_groups": duplicate_groups,
        "duplicate_rows": duplicate_rows,
        "duplicate_row_pct": (round(100.0 * duplicate_rows / len(parsed), 1) if parsed else 0.0),
        "multi_writer_hours": multi_writer_hours,
        "gaps": sorted(gaps, key=lambda g: g["gap_start"]),
        "first_ts": parsed[0][0].isoformat() if parsed else None,
        "last_ts": max(ts for ts, _, _ in parsed).isoformat() if parsed else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判断ジャーナルの重複・欠損監査(読み取り専用)")
    parser.add_argument("journal", help="監査対象のJSONLパス")
    parser.add_argument(
        "--expected-interval-hours",
        type=float,
        default=1.0,
        help="正規の書込み周期(時間)。この2倍を超える間隔を欠損として記録",
    )
    parser.add_argument("--output", help="レポートJSONの書き出し先(省略時は標準出力のみ)")
    args = parser.parse_args(argv)

    path = Path(args.journal)
    rows = read_journal(path)
    report = audit_journal(rows, expected_interval_hours=args.expected_interval_hours)
    report["journal"] = str(path)
    report["audited_at"] = datetime.now(UTC).isoformat()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        f"[gap_audit] {path.name}: {report['total_rows']}行 / 重複{report['duplicate_rows']}行"
        f"({report['duplicate_row_pct']}%) / 欠損{len(report['gaps'])}区間 / "
        f"時刻逆転{report['time_reversals']}件",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
