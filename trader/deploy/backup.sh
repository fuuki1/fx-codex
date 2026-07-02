#!/usr/bin/env bash
# TimescaleDB の論理バックアップ（pg_dump）を取得し、世代を保持する。
# 手動: ./deploy/backup.sh   /   定期: launchd com.trader.backup.plist が毎日実行。
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1   # -> trader/
[ -f .env ] && { set -a; . ./.env; set +a; }

KEEP=${BACKUP_KEEP:-14}              # 保持世代数
DIR=backups
mkdir -p "$DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
FILE="$DIR/trader-$STAMP.sql.gz"

docker compose exec -T timescaledb \
  pg_dump -U "${POSTGRES_USER:-trader}" "${POSTGRES_DB:-trader}" | gzip > "$FILE"

echo "backup -> $FILE ($(du -h "$FILE" | cut -f1))"

# 古い世代を削除（新しい KEEP 個を残す）
ls -1t "$DIR"/trader-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
