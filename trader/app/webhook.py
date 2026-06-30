"""① 外部シグナル受信（FastAPI）。

エンドポイント:
  GET  /health  : 死活確認（monitor / docker healthcheck が叩く）
  POST /webhook : TradingView 等からのシグナル受信

セキュリティ 2 重チェック:
  1. 送信元 IP を TV_ALLOWED_IPS と照合（ngrok 経由は X-Forwarded-For の
     「右端＝信頼プロキシが付与した実クライアント」を見る。偽の XFF で迂回されない）
  2. ペイロードの secret を WEBHOOK_SECRET と定時間比較（hmac.compare_digest）

ボディ解釈:
  TradingView の Webhook は本文を ``Content-Type: text/plain`` で送る。
  FastAPI の ``Body(dict)`` は application/json 以外を受け付けず 422 になるため、
  ここでは Content-Type に依存せず生ボディを自前で JSON パースする。

冪等・鮮度・取りこぼし防止:
  - idem を Redis に nx,ex=3600 で記録。60 分以内の重複は黙って捨てる。
  - {{timenow}} 付きの古い／未来すぎるシグナルは 409（リプレイ・遅延配信の発注を防ぐ）。
  - Stream への publish に失敗したら idem を解放し 503 を返す（再送で復旧でき、
    「dedup に食われて永久ロスト」を防ぐ）。

ブロッキング I/O（Redis/DB）はスレッドプールへ退避し、イベントループを塞がない。
"""
from __future__ import annotations

import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import common
from config import settings
from domain import SignalError, normalize_signal, signal_is_stale
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from logging_setup import log_extra, set_correlation_id, setup_logging

log = setup_logging("webhook", settings.log_level)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    mode = "LIVE" if settings.live_enabled else "paper"
    log.info("webhook started", **log_extra(mode=mode))
    common.log_event("webhook_start", {"mode": mode})
    yield


app = FastAPI(title="trader-webhook", docs_url=None, redoc_url=None, lifespan=lifespan)


def _client_ip(request: Request) -> str:
    """信頼プロキシ段数を考慮して実クライアント IP を返す。

    ngrok 等のプロキシ 1 段（tv_trusted_proxy_hops=1）なら XFF の右端が実クライアント。
    クライアントが偽の XFF を先頭に足しても、プロキシが本物を右に追記するため迂回できない。
    hops=0 なら XFF を無視して TCP ピア IP を使う（プロキシ無しの直接公開時）。
    """
    hops = settings.tv_trusted_proxy_hops
    if hops > 0:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[max(0, len(parts) - hops)]
    return request.client.host if request.client else ""


@app.get("/health")
def health() -> JSONResponse:
    try:
        ok_redis = bool(common.r().ping())
    except Exception:
        ok_redis = False
    ok_db = common.db_ping()
    common.heartbeat("webhook")
    # webhook の本質機能は「シグナルを Stream に publish」なので Redis 必須。
    # DB は best-effort（落ちても受信自体は継続）なので body で報告するだけ。
    healthy = ok_redis
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "redis": ok_redis, "db": ok_db},
        status_code=200 if healthy else 503,
    )


def _process(raw: bytes, ip: str) -> dict:
    """同期パイプライン（IP→secret→JSON→正規化→鮮度→冪等→配信）。

    ブロッキングする Redis/DB を含むためスレッドプールで実行される。
    拒否は HTTPException で表現し、FastAPI が適切な HTTP ステータスへ変換する。
    """
    # 1) IP 検証
    if settings.tv_allowed_ips and ip not in settings.tv_allowed_ips:
        log.warning("rejected by ip", **log_extra(ip=ip))
        raise HTTPException(status_code=403, detail="forbidden")

    # 2) JSON パース（Content-Type 非依存）
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("invalid json body", **log_extra(ip=ip, error=str(e)))
        raise HTTPException(status_code=400, detail="invalid json") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object required")

    # 3) secret 検証（未設定は受信拒否＝安全側）
    if not settings.webhook_secret:
        log.error("WEBHOOK_SECRET not configured -> refusing signals")
        raise HTTPException(status_code=503, detail="server not configured")
    secret = payload.get("secret")
    if not (isinstance(secret, str) and hmac.compare_digest(secret, settings.webhook_secret)):
        log.warning("rejected by secret", **log_extra(ip=ip))
        raise HTTPException(status_code=401, detail="unauthorized")

    # 4) 正規化
    try:
        sig = normalize_signal(payload, source="tradingview")
    except SignalError as e:
        log.warning("invalid signal", **log_extra(error=str(e)))
        raise HTTPException(status_code=400, detail=str(e)) from e

    set_correlation_id(sig["idem"])

    # 5) 鮮度（リプレイ・遅延配信の発注を防ぐ。時刻フィールドが無ければ受信時刻で常に新鮮）
    if signal_is_stale(sig["ts"], time.time(), settings.max_signal_age_sec):
        log.warning(
            "rejected stale signal",
            **log_extra(idem=sig["idem"], ts=sig["ts"], max_age=settings.max_signal_age_sec),
        )
        common.log_event("signal_stale", sig)
        raise HTTPException(status_code=409, detail="stale signal")

    # 6) 冪等（60 分以内の重複を排除）
    idem_key = f"idem:{sig['idem']}"
    try:
        fresh = common.r().set(idem_key, "1", nx=True, ex=3600)
    except Exception as e:
        # Redis 不通では冪等保証も配信もできない。受け付けず 503（TV 側/手動で再送可能に）。
        log.exception("redis unavailable on dedup", **log_extra(idem=sig["idem"]))
        raise HTTPException(status_code=503, detail="queue unavailable") from e
    if not fresh:
        log.info("duplicate ignored", **log_extra(idem=sig["idem"]))
        return {"status": "duplicate_ignored", "idem": sig["idem"]}

    # 7) 配信。失敗したら idem を解放して 503（再送で復旧でき、永久ロストを防ぐ）。
    common.log_event("signal_received", sig)
    try:
        msg_id = common.publish(common.STREAM_SIGNALS, sig)
    except Exception as e:
        try:
            common.r().delete(idem_key)
        except Exception:
            log.exception("failed to release idem after publish error")
        common.notify(
            f"⚠️ シグナル配信失敗（Redis publish）idem={sig['idem']}。再送が必要。",
            key="publish_fail",
        )
        log.exception("publish failed -> released idem", **log_extra(idem=sig["idem"]))
        raise HTTPException(status_code=503, detail="enqueue failed") from e

    log.info("signal accepted", **log_extra(idem=sig["idem"], symbol=sig["symbol"], msg_id=msg_id))
    return {"status": "accepted", "idem": sig["idem"]}


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    # ボディサイズ上限（安価な DoS 対策）。Content-Length で早期に弾き、読み切り後も再確認。
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > settings.max_webhook_body_bytes:
        raise HTTPException(status_code=413, detail="payload too large")
    raw = await request.body()
    if len(raw) > settings.max_webhook_body_bytes:
        raise HTTPException(status_code=413, detail="payload too large")
    ip = _client_ip(request)
    # ブロッキング処理（Redis/DB）はスレッドプールへ。イベントループを塞がない。
    return await run_in_threadpool(_process, raw, ip)
