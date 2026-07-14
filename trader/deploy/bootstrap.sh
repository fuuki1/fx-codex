#!/usr/bin/env bash
# Mac mini を取引サーバー化する初期セットアップ。
#   ./deploy/bootstrap.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"   # -> trader/
cd "$HERE"

echo "== trader bootstrap =="
command -v docker >/dev/null || { echo "ERROR: Docker が必要です（Docker Desktop 等）"; exit 1; }
mkdir -p logs backups

# --- .env ---
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "-> .env を作成（chmod 600）。値を編集してください。"
else
  chmod 600 .env || true
  echo "-> 既存の .env を使用（chmod 600 を再適用）。"
fi

# --- launchd LaunchAgent を設置 ---
mkdir -p "$HOME/Library/LaunchAgents"
for label in com.trader.supervisor com.trader.backup; do
  src="deploy/$label.plist"
  dst="$HOME/Library/LaunchAgents/$label.plist"
  sed "s#__TRADER_DIR__#$HERE#g" "$src" > "$dst"
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "-> LaunchAgent 設置: $dst"
done

cat <<EOF

✅ セットアップ完了。次の手順で Mac mini を無人サーバー化:

  1. .env を編集（IBKR / DB / WEBHOOK_SECRET / DISCORD / NGROK 等）
       \$ \${EDITOR:-nano} .env

  2. 省電力・自動起動（要 sudo / GUI 設定）:
       sudo systemsetup -setusingnetworktime on        # NTP 時刻同期（約定時刻の正確性）
       sudo pmset -a sleep 0 disksleep 0 womp 1         # スリープ無効・Wake on LAN
       # システム設定 > ユーザとグループ > 自動ログイン = ON（Docker Desktop は GUI セッションが必要）
       # Docker Desktop > Settings > General > Start Docker Desktop when you log in = ON

  3. 起動と確認:
       make up        # 全サービス起動
       make ps        # 状態
       make logs      # ログ追従
       make kill-status

  ※ 既定は paper（IB Gateway 4002）。本番は .env で TRADING_MODE=live かつ ALLOW_LIVE=1。
EOF
