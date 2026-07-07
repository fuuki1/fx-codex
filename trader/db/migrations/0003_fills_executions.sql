-- 0003: fills を「実約定履歴」にするための列を追加。
--
-- executor は IBKR の execDetails（実約定）を fills に 1 約定 = 1 行で記録するようになった。
--   fill_price : 実際の約定価格（発注時の想定ではない）
--   exec_id    : IBKR 約定 ID。execDetails の冪等キー、commissionReport との突合キー。
-- 既存 DB には本ファイルを適用する:
--   make migrate
--   または:
--   docker compose exec -T timescaledb psql -U $POSTGRES_USER -d $POSTGRES_DB < db/migrations/0003_fills_executions.sql
--
-- すべて IF NOT EXISTS なので何度流しても安全（冪等）。
-- 注記: fills は hypertable のため一意索引は区分キー ts を含める必要がある。exec_id 冪等は
-- アプリ側（存在チェック→INSERT・単一コンシューマ）で担保するので、ここは検索用の
-- 非一意索引のみ張る。
ALTER TABLE fills ADD COLUMN IF NOT EXISTS fill_price double precision;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS exec_id    text;
CREATE INDEX IF NOT EXISTS fills_exec_id_idx ON fills (exec_id);
