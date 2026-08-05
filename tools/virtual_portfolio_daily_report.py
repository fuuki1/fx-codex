#!/usr/bin/env python3
"""Create and optionally deliver one immutable virtual-portfolio daily report."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, time
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.discord_delivery import (  # noqa: E402
    DiscordDeliveryError,
    send_webhook_batch,
)
from fx_intel.virtual_portfolio import (  # noqa: E402
    VirtualPortfolioStore,
    open_virtual_portfolio_reader,
)
from tools.data_freshness_monitor import load_webhook_url  # noqa: E402

TOKYO = ZoneInfo("Asia/Tokyo")
REPORT_CONTRACT = "virtual-portfolio-daily-report-v2"
DEFAULT_DB = REPO_ROOT / "logs" / "fx_virtual_portfolio.sqlite3"
DEFAULT_RUN_ROOT = REPO_ROOT / "runs" / "virtual_portfolio_daily"
DEFAULT_OUTBOX = REPO_ROOT / "logs" / "virtual_portfolio_daily_discord_outbox.json"


class DailyReportUnavailable(RuntimeError):
    """The requested session is not closed and reconciled yet."""


class DailyReportNotReady(DailyReportUnavailable):
    """A normal delayed-data state that may be retried by the next cycle."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _session_bounds(session_date_jst: str) -> tuple[datetime, datetime]:
    try:
        session_date = datetime.strptime(session_date_jst, "%Y-%m-%d").date()
    except ValueError as error:
        raise DailyReportUnavailable("session date must be YYYY-MM-DD") from error
    start = datetime.combine(session_date, time.min, tzinfo=TOKYO).astimezone(UTC)
    end = datetime.combine(session_date, time.max, tzinfo=TOKYO).astimezone(UTC)
    return start, end


def build_daily_report(
    store: VirtualPortfolioStore,
    *,
    session_date_jst: str,
    generated_at: datetime,
) -> dict[str, object]:
    """Build a reconciled report only after a persisted close request completes."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise DailyReportUnavailable("generated_at must be timezone-aware")
    moment = generated_at.astimezone(UTC)
    close_request = store.session_close_request(session_date_jst=session_date_jst)
    if close_request is None:
        raise DailyReportNotReady("session close request is not recorded")
    completion = store.session_close_completion(
        request_id=str(close_request["request_id"]),
        as_of=moment,
    )
    if completion is None:
        raise DailyReportNotReady("session close request is not complete")
    closed_at = datetime.fromisoformat(str(completion["completed_at"])).astimezone(UTC)
    if closed_at > moment:
        raise DailyReportUnavailable("session close completion is in the future")

    snapshot = store.snapshot(now=closed_at, trade_limit=500)
    if int(snapshot["open_position_count"]) != 0:
        raise DailyReportNotReady("open simulated positions remain")
    reconciliation = snapshot.get("reconciliation")
    if not isinstance(reconciliation, Mapping) or reconciliation.get("passed") is not True:
        raise DailyReportUnavailable("canonical ledger reconciliation failed")

    start, session_end = _session_bounds(session_date_jst)
    sealed_end = min(session_end, closed_at)
    rows = store.connection.execute(
        """
        SELECT o.trade_id, o.symbol, o.timeframe, o.direction, o.planned_risk_jpy,
               c.close_reason, c.gross_market_pnl_jpy, c.spread_quote_cost_jpy,
               c.slippage_cost_jpy, c.commission_jpy, c.financing_jpy,
               c.conversion_cost_jpy, c.net_pnl_jpy, c.realized_net_r,
               c.failure_json
        FROM simulated_trade_opens o
        JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
        JOIN simulated_trade_close_observations ck ON ck.trade_id = c.trade_id
        WHERE c.close_time_ns >= ? AND c.close_time_ns <= ?
          AND ck.close_known_at_ns <= ?
        ORDER BY c.close_time_ns, o.trade_id
        """,
        (
            int(start.timestamp() * 1_000_000_000),
            int(sealed_end.timestamp() * 1_000_000_000),
            int(closed_at.timestamp() * 1_000_000_000),
        ),
    ).fetchall()
    totals = {
        "gross_market_pnl_jpy": 0,
        "spread_quote_cost_jpy": 0,
        "slippage_cost_jpy": 0,
        "commission_jpy": 0,
        "financing_jpy": 0,
        "conversion_cost_jpy": 0,
        "net_pnl_jpy": 0,
    }
    failure_counts: Counter[str] = Counter()
    close_reason_counts: Counter[str] = Counter()
    trades: list[dict[str, object]] = []
    for row in rows:
        for field in totals:
            totals[field] += int(row[field])
        failures = json.loads(str(row["failure_json"]))
        if isinstance(failures, list):
            failure_counts.update(
                str(failure.get("code") or "unknown")
                for failure in failures
                if isinstance(failure, Mapping)
            )
        close_reason_counts[str(row["close_reason"])] += 1
        trades.append(
            {
                "trade_id": str(row["trade_id"]),
                "symbol": str(row["symbol"]),
                "timeframe": str(row["timeframe"]),
                "direction": str(row["direction"]),
                "planned_risk_jpy": int(row["planned_risk_jpy"]),
                "close_reason": str(row["close_reason"]),
                "net_pnl_jpy": int(row["net_pnl_jpy"]),
                "realized_net_r": float(row["realized_net_r"]),
            }
        )
    expected_net = (
        totals["gross_market_pnl_jpy"]
        - totals["spread_quote_cost_jpy"]
        - totals["slippage_cost_jpy"]
        - totals["commission_jpy"]
        - totals["financing_jpy"]
        - totals["conversion_cost_jpy"]
    )
    if expected_net != totals["net_pnl_jpy"]:
        raise DailyReportUnavailable("daily P&L attribution does not reconcile")

    payload: dict[str, object] = {
        "contract": REPORT_CONTRACT,
        "generated_at": closed_at.isoformat(),
        "session_date_jst": session_date_jst,
        "execution_mode": "offline_simulation",
        "broker_connected": False,
        "research_only": True,
        "performance_claim_status": "evaluation_unavailable",
        "daily_target_is_kpi_only": False,
        "daily_target_required_task": True,
        "daily_target_not_optimization_input": True,
        "daily_target_reference_status": "required_operational_task",
        "daily_profit_task": dict(snapshot["daily_profit_task"]),
        "session_close_request": dict(close_request),
        "session_close_completion": dict(completion),
        "horizon_allocation": dict(snapshot["horizon_allocation"]),
        "research_kpis": dict(snapshot["research_kpis"]),
        "portfolio": {
            "initial_capital_jpy": snapshot["initial_capital_jpy"],
            "balance_jpy": snapshot["balance_jpy"],
            "cash_jpy": snapshot["cash_jpy"],
            "equity_jpy": snapshot["equity_jpy"],
            "cumulative_net_pnl_jpy": snapshot["cumulative_net_pnl_jpy"],
            "today_pnl_jpy": totals["net_pnl_jpy"],
            "weekly_pnl_jpy": snapshot["weekly_pnl_jpy"],
            "monthly_pnl_jpy": snapshot["monthly_pnl_jpy"],
            "daily_target_jpy": snapshot["daily_target_jpy"],
            "target_progress": (totals["net_pnl_jpy"] / int(str(snapshot["daily_target_jpy"]))),
            "drawdown_jpy": snapshot["drawdown_jpy"],
            "reconciliation": dict(reconciliation),
        },
        "trade_count": len(trades),
        "trades": trades,
        "pnl_attribution": totals,
        "pnl_identity_passed": True,
        "shortfall_attribution": store.shortfall_attribution(
            session_start=start,
            session_end=sealed_end,
            realized_net_pnl_jpy=totals["net_pnl_jpy"],
            total_cost_jpy=(
                totals["spread_quote_cost_jpy"]
                + totals["slippage_cost_jpy"]
                + totals["commission_jpy"]
                + totals["financing_jpy"]
                + totals["conversion_cost_jpy"]
            ),
            gross_market_pnl_jpy=totals["gross_market_pnl_jpy"],
            trade_count=len(trades),
        ),
        "close_reason_counts": dict(sorted(close_reason_counts.items())),
        "failure_counts": dict(sorted(failure_counts.items())),
        "learning": dict(snapshot["learning"]),
    }
    payload["report_sha256"] = _sha256(payload)
    return payload


def _write_create_only(path: Path, payload: object) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return True


def create_or_load_report(
    run_root: Path,
    report: dict[str, object],
) -> tuple[Path, dict[str, object], bool]:
    session_date = str(report["session_date_jst"])
    path = run_root.resolve() / session_date / "daily_report.json"
    created = _write_create_only(path, report)
    if created:
        return path, report, True
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DailyReportUnavailable("existing daily report cannot be verified") from error
    if not isinstance(existing, dict):
        raise DailyReportUnavailable("existing daily report cannot be verified")
    existing_without_sha = dict(existing)
    existing_sha = existing_without_sha.pop("report_sha256", None)
    if not isinstance(existing_sha, str) or existing_sha != _sha256(existing_without_sha):
        raise DailyReportUnavailable("existing daily report cannot be verified")
    canonical_keys = (
        "contract",
        "generated_at",
        "session_date_jst",
        "execution_mode",
        "broker_connected",
        "research_only",
        "session_close_request",
        "session_close_completion",
        "portfolio",
        "trade_count",
        "trades",
        "pnl_attribution",
        "pnl_identity_passed",
        "close_reason_counts",
        "failure_counts",
    )
    existing_canonical = {key: existing.get(key) for key in canonical_keys}
    current_canonical = {key: report.get(key) for key in canonical_keys}
    if _sha256(existing_canonical) != _sha256(current_canonical):
        raise DailyReportUnavailable(
            "immutable daily report conflicts with the current canonical ledger"
        )
    return path, existing, False


def discord_payload(report: Mapping[str, object]) -> dict[str, object]:
    portfolio = report.get("portfolio")
    attribution = report.get("pnl_attribution")
    failures = report.get("failure_counts")
    learning = report.get("learning")
    research_kpis = report.get("research_kpis")
    assert isinstance(portfolio, Mapping)
    assert isinstance(attribution, Mapping)
    assert isinstance(failures, Mapping)
    assert isinstance(learning, Mapping)
    assert isinstance(research_kpis, Mapping)
    rolling = research_kpis.get("rolling_20d")
    assert isinstance(rolling, Mapping)
    failure_text = (
        " / ".join(f"{key}:{value}" for key, value in list(failures.items())[:5]) or "なし"
    )
    description = "\n".join(
        (
            "**分析専用・ブローカー未接続**",
            f"残高: ¥{int(portfolio['balance_jpy']):,}",
            f"当日純損益: {int(portfolio['today_pnl_jpy']):+,}円",
            f"20日ローリング純R (15m/1hのみ): {float(rolling['net_r']):+.2f}R "
            f"/ n={int(rolling['trade_count'])}",
            f"粗損益: {int(attribution['gross_market_pnl_jpy']):+,}円",
            "コスト: "
            f"{int(attribution['spread_quote_cost_jpy']) + int(attribution['slippage_cost_jpy']) + int(attribution['commission_jpy']) + int(attribution['financing_jpy']) + int(attribution['conversion_cost_jpy']):,}円",
            f"終了取引: {int(str(report['trade_count']))}件 / 失敗分類: {failure_text}",
            f"学習適格: {int(learning['eligible_rows'])}/{int(learning['minimum_rows'])}件",
            "1日7万円は必須タスク。未達は未達として記録し、"
            "安全上限を越える強制取引・最適化入力・達成保証・学習ラベルにはしません。",
        )
    )
    return {
        "embeds": [
            {
                "title": f"仮想口座 日報 {report['session_date_jst']}",
                "description": description[:4000],
                "color": 0x2ECC71 if int(portfolio["today_pnl_jpy"]) >= 0 else 0xE74C3C,
                "footer": {
                    "text": f"report {str(report['report_sha256'])[:12]} / offline simulation"
                },
            }
        ]
    }


def _latest_session_date(store: VirtualPortfolioStore, now: datetime) -> str:
    latest = store.connection.execute(
        """
        SELECT session_date_jst
        FROM session_close_completions
        WHERE completed_at_ns <= ?
        ORDER BY completed_at_ns DESC, completion_id DESC
        LIMIT 1
        """,
        (int(now.astimezone(UTC).timestamp() * 1_000_000_000),),
    ).fetchone()
    if latest is None:
        raise DailyReportNotReady("no completed session is available")
    return str(latest["session_date_jst"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a reconciled analysis-only virtual-portfolio daily report"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--outbox", type=Path, default=DEFAULT_OUTBOX)
    parser.add_argument("--session-date")
    parser.add_argument("--send-discord", action="store_true")
    parser.add_argument(
        "--if-ready",
        action="store_true",
        help="exit successfully when delayed settlement has not completed yet",
    )
    args = parser.parse_args()
    now = datetime.now(UTC)
    try:
        with open_virtual_portfolio_reader(args.db) as store:
            session_date = args.session_date or _latest_session_date(store, now)
            report = build_daily_report(
                store,
                session_date_jst=session_date,
                generated_at=now,
            )
        path, report, created = create_or_load_report(args.run_root, report)
        receipt = path.with_name("discord_delivery.json")
        delivered = receipt.is_file()
        if args.send_discord and not delivered:
            webhook_url = load_webhook_url(args.root.resolve())
            if not webhook_url:
                raise DailyReportUnavailable("Discord webhook is not configured")
            send_webhook_batch(
                webhook_url,
                [discord_payload(report)],
                outbox_path=args.outbox,
            )
            delivered = (
                _write_create_only(
                    receipt,
                    {
                        "contract": "virtual-portfolio-daily-discord-delivery-v1",
                        "delivered_at": datetime.now(UTC).isoformat(),
                        "report_sha256": report["report_sha256"],
                    },
                )
                or receipt.is_file()
            )
    except DailyReportNotReady as error:
        print(
            json.dumps(
                {"ok": True, "status": "not_ready", "reason": str(error)},
                ensure_ascii=False,
            )
        )
        return 0 if args.if_ready else 2
    except (
        DailyReportUnavailable,
        DiscordDeliveryError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "artifact": str(path),
                "created": created,
                "discord_delivered": delivered,
                "report_sha256": report["report_sha256"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
