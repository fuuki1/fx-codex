"""⑥ 死活監視・日次通知。

60 秒ごとに:
  1. webhook /health の HTTP 疎通
  2. DB（SELECT 1）
  3. Redis（PING）
  4. 各サービスのハートビート鮮度（= ハング検知。プロセスは生きてるのに
     ループが止まっている状態を捉える）
異常はcommon.notifyへ渡す。signal_boardモードではログだけに残り、allモードでは
スロットル付きでDiscordへも通知する。

毎朝 7 時（JST）に日次サマリを送信。送信済みフラグを Redis に持ち 1 日 1 回。
"""
from __future__ import annotations

from datetime import datetime

import common
import httpx
from config import settings
from domain import JST
from logging_setup import log_extra, setup_logging

log = setup_logging("monitor", settings.log_level)

EXPECTED_SERVICES = ["webhook", "risk", "executor", "strategy"]
HEARTBEAT_STALE_SEC = 180
WEBHOOK_HEALTH_URL = "http://webhook:8000/health"
LOOP_SEC = 60
DAILY_SUMMARY_HOUR = 7
KEY_SUMMARY_SENT = "monitor:daily_summary_date"


def check_webhook() -> bool:
    try:
        resp = httpx.get(WEBHOOK_HEALTH_URL, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def check_heartbeats(now: float) -> list[str]:
    beats = common.read_heartbeats()
    stale = []
    for svc in EXPECTED_SERVICES:
        ts = beats.get(svc)
        if ts is None or (now - ts) > HEARTBEAT_STALE_SEC:
            age = "missing" if ts is None else f"{int(now - ts)}s"
            stale.append(f"{svc}({age})")
    return stale


def run_checks() -> None:
    import time

    now = time.time()
    problems = []

    if not check_webhook():
        problems.append("webhook /health 不通")
    if not common.db_ping():
        problems.append("DB 不通")
    try:
        if not common.r().ping():
            problems.append("Redis 不通")
    except Exception:
        problems.append("Redis 不通")

    stale = check_heartbeats(now)
    if stale:
        problems.append("ハートビート停止: " + ", ".join(stale))

    if problems:
        common.notify("🚨 監視異常:\n- " + "\n- ".join(problems), key="monitor:" + "|".join(problems))
        log.warning("health problems", **log_extra(problems=problems))
    else:
        log.info("all healthy")


def maybe_daily_summary() -> None:
    now_jst = datetime.now(JST)
    if now_jst.hour != DAILY_SUMMARY_HOUR:
        return
    today = now_jst.strftime("%Y-%m-%d")
    # 当日まだ送っていなければ送る（NX で 25 時間有効に）
    if not common.r().set(KEY_SUMMARY_SENT, today, nx=True, ex=90_000):
        if common.r().get(KEY_SUMMARY_SENT) == today:
            return
        common.r().set(KEY_SUMMARY_SENT, today, ex=90_000)

    try:
        # risk._today_realized_pnl と同じ JST 日界で集計する（UTC のままだと 9 時 JST リセット）
        rows = common.db_query(
            "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0) FROM fills "
            "WHERE ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'Asia/Tokyo'"
        )
        n, pnl = rows[0]
    except Exception:
        n, pnl = 0, 0
    ks = "ON" if common.kill_switch_on() else "OFF"
    common.notify(
        f"📊 日次: 約定{n}件 / 実現損益 {float(pnl):.0f} / KillSwitch {ks} / mode {settings.trading_mode}",
        throttle=False,
    )
    log.info("daily summary sent", **log_extra(fills=n, pnl=float(pnl)))


def main() -> None:
    stop = common.install_signal_handlers()
    log.info("monitor starting")
    while not stop.is_set():
        common.heartbeat("monitor")
        try:
            run_checks()
            maybe_daily_summary()
        except Exception:
            log.exception("monitor loop error")
        stop.wait(LOOP_SEC)
    log.info("monitor stopped")


if __name__ == "__main__":
    main()
