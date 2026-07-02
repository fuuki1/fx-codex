-- TimescaleDB スキーマ初期化。
-- timescaledb イメージの docker-entrypoint-initdb.d で初回のみ実行される。
-- 冪等に書く（IF NOT EXISTS）ことで手動再適用にも耐える。

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================================
-- events : 全イベントの監査ログ（append-only）
-- ============================================================================
CREATE TABLE IF NOT EXISTS events (
    ts      timestamptz NOT NULL DEFAULT now(),
    kind    text        NOT NULL,
    payload jsonb       NOT NULL DEFAULT '{}'::jsonb
);
SELECT create_hypertable('events', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS events_kind_ts_idx ON events (kind, ts DESC);
-- idem（相関ID）での追跡を速くする
CREATE INDEX IF NOT EXISTS events_idem_idx ON events ((payload->>'idem'));

-- ============================================================================
-- fills : 発注/約定の記録
-- ============================================================================
CREATE TABLE IF NOT EXISTS fills (
    ts           timestamptz NOT NULL DEFAULT now(),
    symbol       text        NOT NULL,
    side         text        NOT NULL,
    qty          double precision NOT NULL,
    status       text        NOT NULL,
    broker       text        NOT NULL DEFAULT 'IBKR',
    ref          text,
    realized_pnl double precision NOT NULL DEFAULT 0,
    idem         text
);
SELECT create_hypertable('fills', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS fills_symbol_ts_idx ON fills (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS fills_ref_idx ON fills (ref);
CREATE INDEX IF NOT EXISTS fills_idem_idx ON fills (idem);

-- ============================================================================
-- processed_orders : 冪等な発注の決め手（exactly-once 近似）
--   executor は発注前にここへ idem を INSERT し、衝突したら二重発注を回避する。
-- ============================================================================
CREATE TABLE IF NOT EXISTS processed_orders (
    idem            text PRIMARY KEY,
    client_order_id text NOT NULL,
    submitted_at    timestamptz NOT NULL DEFAULT now(),
    broker_ref      text,
    status          text NOT NULL DEFAULT 'submitting'
);

-- 日次実現損益（リスク判定で使う）を速く集計するためのビュー
CREATE OR REPLACE VIEW daily_pnl AS
SELECT date_trunc('day', ts) AS day,
       sum(realized_pnl)     AS realized_pnl,
       count(*)              AS fill_count
FROM fills
GROUP BY 1;
