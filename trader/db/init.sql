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

-- リスクエンジン由来の列（プロ級リスク管理 / R 倍数ジャーナル用）。
--   intended_risk : 発注時の想定最大損失（口座通貨）= サイズ × ストップ距離 × 単価
--   stop_distance : 発注時のストップ距離（価格）
--   realized_r    : 実現損益 ÷ intended_risk（= R 倍数。期待値の単位として使う）
-- ADD COLUMN IF NOT EXISTS により既存 DB への再適用にも耐える（migrations/ も参照）。
ALTER TABLE fills ADD COLUMN IF NOT EXISTS intended_risk double precision NOT NULL DEFAULT 0;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS stop_distance double precision;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS realized_r    double precision;

-- 実約定（execDetails）の記録用の列。
--   fill_price : 実際の約定価格（想定値ではない）
--   exec_id    : IBKR の約定 ID（execDetails/commissionReport の冪等・突合キー）
-- fills は「発注ログ」ではなく「実約定履歴」。executor は execDetails を受けて 1 約定 = 1 行を
-- exec_id 冪等で記録し、commissionReport が exec_id 基準で realized_pnl / realized_r を更新する。
ALTER TABLE fills ADD COLUMN IF NOT EXISTS fill_price double precision;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS exec_id    text;
CREATE INDEX IF NOT EXISTS fills_exec_id_idx ON fills (exec_id);

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

-- 日次実現損益の集計ビュー（レポート用）。境界は JST（RISK_DAY_TIMEZONE と揃える）。
-- リスク判定側 risk.py は設定 timezone で都度集計する。ここは表示用のため JST を明示。
CREATE OR REPLACE VIEW daily_pnl AS
SELECT date_trunc('day', ts AT TIME ZONE 'Asia/Tokyo') AS day_jst,
       sum(realized_pnl)                               AS realized_pnl,
       count(*)                                        AS fill_count
FROM fills
GROUP BY 1;
