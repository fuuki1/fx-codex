#!/usr/bin/env bash
# 自律最適化（walk-forward / OOS 検証つき）を実行し strategy_params.json を更新する。
# 手動: ./deploy/optimize.sh   /   定期: launchd com.trader.optimize.plist が週次実行。
# auto_optimize.py 自身が失敗を握りつぶして安全側（既存パラメータ維持）に倒すため -e は付けない。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1   # -> trader/
[ -f .env ] && { set -a; . ./.env; set +a; }

notify() {
  [ -n "${DISCORD_WEBHOOK_URL:-}" ] || return 0
  local payload
  payload=$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$1")
  curl -fsS -m 10 -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

if ! docker info >/dev/null 2>&1; then
  notify "⚠️ optimize: Docker が未起動のためスキップしました。"
  exit 0
fi

OUT=$(python3 optimize/auto_optimize.py 2>&1)
RC=$?
echo "$OUT"

if [ $RC -ne 0 ]; then
  notify "⚠️ optimize: auto_optimize.py が異常終了しました（rc=$RC）。logs/optimize.err.log を確認してください。"
  exit $RC
fi

if echo "$OUT" | grep -q '"deployed": true'; then
  notify "🧪 自律最適化: strategy_params.json を更新しました（詳細: fx-codex/optimize_result.log）。"
else
  notify "🧪 自律最適化: 検証基準を満たさず既存パラメータを維持しました（詳細: fx-codex/optimize_result.log）。"
fi
