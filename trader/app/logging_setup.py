"""構造化(JSON)ロギング。

全サービスが起動時に `setup_logging(service_name)` を呼ぶ。ログは 1 行 1 JSON で
stdout に出る（Docker のログドライバ→集約に向く）。`idem`(相関ID)を contextvar で
持ち回り、signal→risk→order→fill を横断して追跡できる。
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any

# signal の idem を 1 リクエスト/メッセージ処理の間だけ束ねる相関ID
correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

_SERVICE = "app"


class JsonFormatter(logging.Formatter):
    """1 行 1 JSON のフォーマッタ。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": _SERVICE,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = correlation_id.get()
        if cid:
            payload["idem"] = cid
        # extra={"...": ...} で渡された任意フィールドを取り込む
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(service_name: str, level: str = "INFO") -> logging.Logger:
    global _SERVICE
    _SERVICE = service_name

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 外部ライブラリの冗長ログを抑制
    for noisy in ("ib_async", "ib_insync", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(service_name)


def log_extra(**fields: Any) -> dict[str, Any]:
    """logger.info("msg", **log_extra(order_id=...)) のための糖衣。"""
    return {"extra": {"extra_fields": fields}}


def set_correlation_id(idem: str | None) -> None:
    correlation_id.set(idem)
