-- 0002: daily_pnl ビューを JST 境界に変更（RISK_DAY_TIMEZONE と整合）。
--
-- 日次/週次損失リミットは risk.py が RISK_DAY_TIMEZONE（既定 Asia/Tokyo）で集計する。
-- レポート用の daily_pnl ビューも同じ JST 境界に揃える。既存 DB には本ファイルを適用:
--   make migrate
-- CREATE OR REPLACE なので何度流しても安全（冪等）。
CREATE OR REPLACE VIEW daily_pnl AS
SELECT date_trunc('day', ts AT TIME ZONE 'Asia/Tokyo') AS day_jst,
       sum(realized_pnl)                               AS realized_pnl,
       count(*)                                        AS fill_count
FROM fills
GROUP BY 1;
