"""① 外部シグナル受信（FastAPI）。

エンドポイント:
  GET  /health  : 死活確認（monitor / docker healthcheck が叩く）
  POST /webhook : TradingView 等からのシグナル受信

セキュリティ 2 重チェック:
  1. 送信元 IP を TV_ALLOWED_IPS と照合（ngrok 経由は X-Forwarded-For の「末尾」を見る。
     先頭はクライアントが自由に偽装できるため信頼しない）
  2. ペイロードの secret を WEBHOOK_SECRET と定時間比較（hmac.compare_digest）

DoS 対策: Content-Length が MAX_BODY_BYTES を超えるリクエストは 413 で拒否
（uvicorn はボディサイズを制限しないため、アプリ側で上限を設ける）。

冪等: idem を Redis に nx,ex=3600 で記録。60 分以内の重複は黙って捨てる。
ハンドラは同期 def（FastAPI がスレッドプールで実行）なので同期 Redis でも
イベントループを塞がない。
"""
from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import common
from config import settings
from domain import SignalError, normalize_signal
from fastapi import Body, FastAPI, HTTPException, Request
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

# TradingView のアラートは数百バイト程度。これを大きく超えるボディは攻撃とみなす。
MAX_BODY_BYTES = 64 * 1024


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):  # type: ignore[no-untyped-def]
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        return JSONResponse({"detail": "invalid content-length"}, status_code=400)
    if length > MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    return await call_next(request)


def _client_ip(request: Request) -> str:
    """信頼できる送信元 IP を返す。

    X-Forwarded-For はクライアントが任意の値を先頭に付けられ、経路上のプロキシが
    実際の接続元を「右端に追記」する仕様。信頼できるのは末尾（= 直近の信頼プロキシ
    ngrok が追記した実接続元）だけ。先頭を採用すると許可 IP を偽装した
    allowlist バイパスが可能になる。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
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


@app.post("/webhook")
def webhook(request: Request, payload: dict = Body(...)) -> dict:
    ip = _client_ip(request)

    # 1) IP 検証
    if settings.tv_allowed_ips and ip not in settings.tv_allowed_ips:
        log.warning("rejected by ip", **log_extra(ip=ip))
        raise HTTPException(status_code=403, detail="forbidden")

    # 2) secret 検証（未設定は受信拒否＝安全側）
    if not settings.webhook_secret:
        log.error("WEBHOOK_SECRET not configured -> refusing signals")
        raise HTTPException(status_code=503, detail="server not configured")
    secret = payload.get("secret")
    if not (isinstance(secret, str) and hmac.compare_digest(secret, settings.webhook_secret)):
        log.warning("rejected by secret", **log_extra(ip=ip))
        raise HTTPException(status_code=401, detail="unauthorized")

    # 3) 正規化
    try:
        sig = normalize_signal(payload, source="tradingview")
    except SignalError as e:
        log.warning("invalid signal", **log_extra(error=str(e)))
        raise HTTPException(status_code=400, detail=str(e)) from e

    set_correlation_id(sig["idem"])

    # 4) 冪等（60 分以内の重複を排除）
    if not common.r().set(f"idem:{sig['idem']}", "1", nx=True, ex=3600):
        log.info("duplicate ignored", **log_extra(idem=sig["idem"]))
        return {"status": "duplicate_ignored", "idem": sig["idem"]}

    # 5) 記録 + 配信
    common.log_event("signal_received", sig)
    msg_id = common.publish(common.STREAM_SIGNALS, sig)
    log.info("signal accepted", **log_extra(idem=sig["idem"], symbol=sig["symbol"], msg_id=msg_id))
    return {"status": "accepted", "idem": sig["idem"]}
