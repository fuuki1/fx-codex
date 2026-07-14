# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# - PYTHONUNBUFFERED: ログを即時 flush（コンテナログで欠落しない）
# - PYTHONDONTWRITEBYTECODE: .pyc を作らない
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Tokyo

WORKDIR /app

# 依存を先に入れてレイヤキャッシュを効かせる
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# アプリ本体
COPY app/ /app/

# 非 root で実行（最小権限）
RUN useradd --create-home --uid 10001 trader \
    && chown -R trader:trader /app
USER trader

# 既定はヘルプ表示。実際の起動コマンドは docker-compose 側で指定する。
CMD ["python", "-c", "print('specify a command in docker-compose')"]
