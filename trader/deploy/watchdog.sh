#!/usr/bin/env bash
# Mac mini 常駐ウォッチドッグ。launchd から 120 秒ごとに起動される（短命プロセス）。
#  - Docker が動いていなければ起動を促す
#  - `docker compose up -d` で全サービスを冪等に起動
#  - unhealthy / exited を検出したら再起動し、allモードだけDiscord通知
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1   # -> trader/
[ -f .env ] && { set -a; . ./.env; set +a; }

notify() {
  [ "${DISCORD_NOTIFICATION_MODE:-signal_board}" = "all" ] || return 0
  [ -n "${DISCORD_WEBHOOK_URL:-}" ] || return 0
  local payload
  payload=$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$1")
  curl -fsS -m 10 -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

# Docker エンジンの稼働確認
if ! docker info >/dev/null 2>&1; then
  open -a Docker 2>/dev/null || true   # Docker Desktop の起動を試みる
  notify "⚠️ watchdog: Docker が未起動。起動を試行しました。"
  exit 0
fi

# 全サービスを起動（既に起動済みなら何もしない）
docker compose up -d >/dev/null 2>&1

# unhealthy / exited を抽出
bad=$(docker compose ps --format json 2>/dev/null | python3 -c '
import sys, json
out = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    items = obj if isinstance(obj, list) else [obj]
    for it in items:
        if it.get("Health") == "unhealthy" or it.get("State") == "exited":
            out.append(it.get("Service", "?"))
print(" ".join(o for o in out if o))
')

if [ -n "$bad" ]; then
  notify "🔁 watchdog: 異常サービスを再起動します: $bad"
  for s in $bad; do
    docker compose restart "$s" >/dev/null 2>&1 || true
  done
fi
