#!/usr/bin/env bash
# 自律最適化: IB Gateway から実ヒストリカルデータを取得し、取得できた時だけ
# walk-forward / OOS 検証つき最適化（optimize/auto_optimize.py）を実行する。
#   手動: ./deploy/optimize.sh   /   定期: launchd com.trader.optimize.plist が週次実行。
#
# 実データを取得できなければ「最適化しない」。同梱の合成サンプルへはフォールバック
# しない（合成データに過剰適合したパラメータが strategy_params.json 経由でライブ戦略へ
# 自動配備される事故を防ぐ、auto_optimize.py の OPTIMIZE_DATA 必須方針を維持する）。
#
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

DATA_DIR="${OPTIMIZE_DATA_DIR:-$PWD/data}"
HISTORY_CSV="$DATA_DIR/history.csv"
SYMBOL="${STRATEGY_SYMBOL:-USDJPY}"
ASSET="${STRATEGY_ASSET:-fx}"
YEARS="${FXBT_HISTORY_YEARS:-5}"
mkdir -p "$DATA_DIR"

# IB Gateway から実データを取得（executor サービス定義 = env/ネットワークを借用。
# `make reconcile` と同じ手法。失敗時は既存 CSV を変更せず非ゼロ終了する）。
if ! docker compose run --rm --no-deps \
      -v "$DATA_DIR:/fx-codex-data" \
      executor python export_history.py \
      --out /fx-codex-data/history.csv \
      --symbol "$SYMBOL" --asset "$ASSET" --years "$YEARS"; then
  notify "⚠️ optimize: 実ヒストリカルデータを取得できなかったため最適化をスキップしました（合成サンプルへはフォールバックしません）。"
  exit 0
fi

OUT=$(OPTIMIZE_DATA="$HISTORY_CSV" python3 optimize/auto_optimize.py 2>&1)
RC=$?
echo "$OUT"

if [ $RC -ne 0 ]; then
  notify "⚠️ optimize: auto_optimize.py が異常終了しました（rc=$RC）。logs/optimize.err.log を確認してください。"
  exit $RC
fi

if echo "$OUT" | grep -q '"deployed": true'; then
  notify "🧪 自律最適化: strategy_params.json を更新しました（データ: $HISTORY_CSV）。"
else
  notify "🧪 自律最適化: 検証基準を満たさず既存パラメータを維持しました。"
fi
