-- 0001: fills へリスクエンジン由来の列を追加（プロ級リスク管理 / R 倍数ジャーナル）。
--
-- init.sql は新規ボリュームの初回のみ実行される。既存 DB には本ファイルを適用する:
--   make migrate            （docker compose 経由）
--   または:
--   docker compose exec -T timescaledb psql -U $POSTGRES_USER -d $POSTGRES_DB < db/migrations/0001_risk_columns.sql
--
-- すべて IF NOT EXISTS なので何度流しても安全（冪等）。
ALTER TABLE fills ADD COLUMN IF NOT EXISTS intended_risk double precision NOT NULL DEFAULT 0;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS stop_distance double precision;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS realized_r    double precision;
