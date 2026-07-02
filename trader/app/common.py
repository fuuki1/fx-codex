"""全サービスの共通部品。

- DB 接続プール（psycopg_pool）
- Redis クライアント（同期）
- Discord 通知（スロットル付きでアラート嵐を防止）
- Kill switch / ハートビート / イベントログ
- Redis Streams のコンシューマ（XAUTOCLAIM による pending 回収 + dead-letter 退避）

ネットワーク相手（DB/Redis/Discord）はすべて落ちうる前提で、失敗しても
プロセスを巻き込まない（通知や監視は best-effort、発注経路は明示的に扱う）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import redis
from config import settings
from logging_setup import log_extra
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

log = logging.getLogger("common")

# ---- Redis Stream / キー名 -------------------------------------------------
STREAM_SIGNALS = "signals"          # webhook/strategy -> risk
STREAM_ORDERS = "orders"            # risk -> executor
DEAD_SUFFIX = ":dead"               # 毒メッセージ退避先
KEY_KILL_SWITCH = "kill_switch"
KEY_HEARTBEATS = "heartbeats"       # hash: service -> epoch
KEY_CONSEC_ERRORS = "exec:consecutive_errors"
MAX_DELIVERIES = 5                  # この回数を超えて失敗したら dead-letter

# ============================================================================
# Redis
# ============================================================================
_redis: redis.Redis | None = None


def r() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
            retry_on_timeout=True,
        )
    return _redis


# ============================================================================
# DB（接続プール）
# ============================================================================
_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.db_conninfo,
            min_size=1,
            max_size=5,
            max_idle=300,
            timeout=10,
            kwargs={"autocommit": True},
            open=False,
        )
        _pool.open(wait=True, timeout=30)
    return _pool


def db_execute(sql: str, params: tuple | None = None) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def db_query(sql: str, params: tuple | None = None) -> list[tuple]:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def db_ping() -> bool:
    try:
        return db_query("SELECT 1")[0][0] == 1
    except Exception:
        return False


# ============================================================================
# イベントログ
# ============================================================================
def log_event(kind: str, payload: dict[str, Any]) -> None:
    """events テーブルへ全イベントを記録（失敗してもアプリは止めない）。"""
    try:
        db_execute(
            "INSERT INTO events (ts, kind, payload) VALUES (now(), %s, %s)",
            (kind, Json(payload)),
        )
    except Exception:
        log.exception("log_event failed", **log_extra(kind=kind))


# ============================================================================
# 通知（Discord）— スロットル付き
# ============================================================================
def notify(text: str, *, key: str | None = None, throttle: bool = True) -> None:
    """Discord へ通知。同一 key（無ければ本文）は notify_throttle_sec 内は再送しない。

    アラート嵐（同じ障害で毎秒通知）を防ぐのが目的。通知の成否に関わらず必ずログは残す。
    """
    log.warning("NOTIFY: %s", text, **log_extra(notify_key=key))
    url = settings.discord_webhook_url
    if not url:
        return
    if throttle:
        digest = hashlib.sha256((key or text).encode()).hexdigest()[:16]
        # NX で書けた時だけ送る（= 直近 throttle 秒で初回）
        if not r().set(f"notify:throttle:{digest}", "1", nx=True, ex=settings.notify_throttle_sec):
            return
    try:
        httpx.post(url, json={"content": text[:1900]}, timeout=10)
    except Exception:
        log.exception("discord notify failed")


# ============================================================================
# Kill switch
# ============================================================================
def kill_switch_on() -> bool:
    try:
        return r().get(KEY_KILL_SWITCH) == "1"
    except Exception:
        # Redis に届かない時は「安全側＝発注停止」とみなす（fail-safe）
        log.exception("kill_switch read failed -> treat as ON (fail-safe)")
        return True


def set_kill_switch(on: bool, *, reason: str = "") -> None:
    r().set(KEY_KILL_SWITCH, "1" if on else "0")
    log_event("kill_switch", {"on": on, "reason": reason})


# ============================================================================
# ハートビート（monitor が鮮度を見てハングを検知）
# ============================================================================
def heartbeat(service: str) -> None:
    try:
        r().hset(KEY_HEARTBEATS, service, str(time.time()))
    except Exception:
        log.exception("heartbeat failed", **log_extra(service=service))


def read_heartbeats() -> dict[str, float]:
    try:
        return {k: float(v) for k, v in r().hgetall(KEY_HEARTBEATS).items()}
    except Exception:
        return {}


# ============================================================================
# Redis Streams
# ============================================================================
def ensure_group(stream: str, group: str) -> None:
    try:
        r().xgroup_create(stream, group, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def publish(stream: str, obj: dict[str, Any]) -> str:
    """Stream へ 1 メッセージ追加（payload は JSON 文字列で格納）。"""
    return r().xadd(stream, {"data": json.dumps(obj, default=str)})


def _parse(fields: dict[str, str]) -> dict[str, Any]:
    return json.loads(fields["data"])


def consume(
    stream: str,
    group: str,
    consumer: str,
    handler: Callable[[dict[str, Any]], None],
    stop: threading.Event,
    *,
    service: str,
    block_ms: int = 5000,
    idle_reclaim_ms: int = 60_000,
    on_idle: Callable[[], None] | None = None,
) -> None:
    """コンシューマグループでメッセージを処理する（クラッシュ復旧つき）。

    - 成功時のみ XACK（処理途中で落ちても取りこぼさない / at-least-once）。
    - 起動毎/定期的に XAUTOCLAIM で他コンシューマの宙づり pending を回収。
    - 同一メッセージが MAX_DELIVERIES 回失敗したら dead-letter stream へ退避し ACK。
    - ループ毎にハートビートを打つ（monitor がハングを検知できる）。
    """
    ensure_group(stream, group)
    log.info("consumer started", **log_extra(stream=stream, group=group, consumer=consumer))

    while not stop.is_set():
        try:
            heartbeat(service)
            # 1) 宙づり pending を回収（前回クラッシュ分や他コンシューマの取りこぼし）
            _, claimed, _ = r().xautoclaim(
                stream, group, consumer, min_idle_time=idle_reclaim_ms, start_id="0-0", count=10
            )
            for msg_id, fields in claimed:
                _handle_one(stream, group, msg_id, fields, handler)

            # 2) 新規メッセージ
            resp = r().xreadgroup(group, consumer, {stream: ">"}, count=10, block=block_ms)
            for _stream, messages in resp or []:
                for msg_id, fields in messages:
                    _handle_one(stream, group, msg_id, fields, handler)

            # 3) アイドル時フック（executor が IB のイベントループを回す等）
            if on_idle is not None:
                on_idle()
        except redis.RedisError:
            log.exception("redis error in consume loop; backing off")
            stop.wait(2.0)
        except Exception:
            log.exception("unexpected error in consume loop; backing off")
            stop.wait(2.0)


def _handle_one(
    stream: str,
    group: str,
    msg_id: str,
    fields: dict[str, str],
    handler: Callable[[dict[str, Any]], None],
) -> None:
    try:
        obj = _parse(fields)
    except Exception:
        # パース不能（壊れたメッセージ）は即 dead-letter
        log.exception("undecodable message -> dead-letter", **log_extra(msg_id=msg_id))
        r().xadd(stream + DEAD_SUFFIX, {**fields, "_reason": "decode_error", "_src": msg_id})
        r().xack(stream, group, msg_id)
        return

    try:
        handler(obj)
        r().xack(stream, group, msg_id)
    except Exception:
        attempts = r().hincrby(f"attempts:{stream}", msg_id, 1)
        log.exception(
            "handler failed",
            **log_extra(msg_id=msg_id, attempts=attempts),
        )
        if attempts >= MAX_DELIVERIES:
            r().xadd(
                stream + DEAD_SUFFIX,
                {"data": fields["data"], "_reason": "max_deliveries", "_src": msg_id},
            )
            r().xack(stream, group, msg_id)
            r().hdel(f"attempts:{stream}", msg_id)
            notify(
                f"☠️ メッセージを dead-letter へ退避 stream={stream} id={msg_id}",
                key=f"deadletter:{stream}",
            )
        # それ以外は ACK しない → idle_reclaim 後に再処理される


# ============================================================================
# グレースフル停止
# ============================================================================
def install_signal_handlers() -> threading.Event:
    """SIGTERM/SIGINT で立つ停止イベントを返す。各サービスはこれを監視して終了する。"""
    stop = threading.Event()

    def _handler(signum: int, _frame: Any) -> None:
        log.info("signal received -> shutting down", **log_extra(signal=signum))
        stop.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    return stop
