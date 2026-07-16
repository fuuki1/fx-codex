# E2E学習ループ監査レポート

- 生成時刻: 2026-07-16T05:49:32.453978+00:00
- 対象: `logs` / 窓 72h
- **総合判定: WARN**

| セクション | 判定 | 概要 |
|---|---|---|
| data_collection | warn | 窓内に最大979分の収集ギャップ、5分スロットの実効カバレッジ最小39% |
| prediction_capture | pass | 時間足別・融合の予測記録は継続 |
| outcome_maturation | pass | 満期1979件中1869件を採点済み |
| trade_outcome | pass | MFE/MAE/TP/SL先着の期待値監査が生成されている |
| learning_update | pass | 学習セル12件が採点済み(重み調整9/確信度減衰2/条件補正9) |
| decision_application | pass | 非既定重み3974行/期待値注入0行を確認 |
| duplicate_detection | warn | errログにduplicate writer痕跡1件(競合ガード発火) |
| freshness | pass | 鮮度レポート4分前・対象status={'tf_price_snapshot': 'ok', 'tf_journal': 'ok'} |
| scanner_429 | warn | 末尾200行に429/decode痕跡151件(時刻無し。現在の価格収集は健全のため修正前の残存と推定。ログrotateで解消可) |
| sample_sufficiency | pass | 重み学習可能セル12件/減衰可能セル12件 |
| blocking_reasons | pass | 負期待値の判断行0件(窓内) |
| launchd | warn | com.fx-codex.briefing の最終exit=5 |

## 証拠(セクション別)

### data_collection
```json
{
  "path": "logs/briefing_tf_prices.jsonl",
  "exists": true,
  "total_lines": 22386,
  "malformed_lines": 0,
  "window_open_hours": 72.0,
  "expected_5m_slots_per_cell": 864.0,
  "cells": {
    "EURUSD:15m": {
      "rows": 453,
      "distinct_5m_slots": 453,
      "window_coverage_ratio": 0.5243,
      "effective_coverage_ratio": 0.5243,
      "first_ts": "2026-07-13T05:49:46.497724+00:00",
      "max_open_market_gap_minutes": 978.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "EURUSD:1d": {
      "rows": 460,
      "distinct_5m_slots": 460,
      "window_coverage_ratio": 0.5324,
      "effective_coverage_ratio": 0.5324,
      "first_ts": "2026-07-13T05:49:46.497724+00:00",
      "max_open_market_gap_minutes": 978.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "EURUSD:1h": {
      "rows": 455,
      "distinct_5m_slots": 455,
      "window_coverage_ratio": 0.5266,
      "effective_coverage_ratio": 0.5266,
      "first_ts": "2026-07-13T05:49:46.497724+00:00",
      "max_open_market_gap_minutes": 978.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "EURUSD:4h": {
      "rows": 458,
      "distinct_5m_slots": 458,
      "window_coverage_ratio": 0.5301,
      "effective_coverage_ratio": 0.5301,
      "first_ts": "2026-07-13T05:49:46.497724+00:00",
      "max_open_market_gap_minutes": 978.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "GBPUSD:15m": {
      "rows": 124,
      "distinct_5m_slots": 124,
      "window_coverage_ratio": 0.1435,
      "effective_coverage_ratio": 0.3949,
      "first_ts": "2026-07-15T03:39:32.588385+00:00",
      "max_open_market_gap_minutes": 152.9,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "GBPUSD:1d": {
      "rows": 131,
      "distinct_5m_slots": 131,
      "window_coverage_ratio": 0.1516,
      "effective_coverage_ratio": 0.4172,
      "first_ts": "2026-07-15T03:39:32.588385+00:00",
      "max_open_market_gap_minutes": 142.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "GBPUSD:1h": {
      "rows": 126,
      "distinct_5m_slots": 126,
      "window_coverage_ratio": 0.1458,
      "effective_coverage_ratio": 0.4013,
      "first_ts": "2026-07-15T03:39:32.588385+00:00",
      "max_open_market_gap_minutes": 152.9,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "GBPUSD:4h": {
      "rows": 129,
      "distinct_5m_slots": 129,
      "window_coverage_ratio": 0.1493,
      "effective_coverage_ratio": 0.4108,
      "first_ts": "2026-07-15T03:39:32.588385+00:00",
      "max_open_market_gap_minutes": 142.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "USDJPY:15m": {
      "rows": 453,
      "distinct_5m_slots": 453,
      "window_coverage_ratio": 0.5243,
      "effective_coverage_ratio": 0.5243,
      "first_ts": "2026-07-13T05:49:46.497724+00:00",
      "max_open_market_gap_minutes": 978.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "USDJPY:1d": {
      "rows": 460,
      "distinct_5m_slots": 460,
      "window_coverage_ratio": 0.5324,
      "effective_coverage_ratio": 0.5324,
      "first_ts": "2026-07-13T05:49:46.497724+00:00",
      "max_open_market_gap_minutes": 978.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "USDJPY:1h": {
      "rows": 455,
      "distinct_5m_slots": 455,
      "window_coverage_ratio": 0.5266,
      "effective_coverage_ratio": 0.5266,
      "first_ts": "2026-07-13T05:49:46.497724+00:00",
      "max_open_market_gap_minutes": 978.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    },
    "USDJPY:4h": {
      "rows": 458,
      "distinct_5m_slots": 458,
      "window_coverage_ratio": 0.5301,
      "effective_coverage_ratio": 0.5301,
      "first_ts": "2026-07-13T05:49:46.497724+00:00",
      "max_open_market_gap_minutes": 978.8,
      "last_ts": "2026-07-16T05:47:09.537161+00:00"
    }
  },
  "min_effective_coverage_ratio": 0.3949,
  "worst_open_market_gap_minutes": 978.8,
  "last_price_age_open_minutes": 2.4
}
```

### prediction_capture
```json
{
  "tf_journal": {
    "path": "logs/briefing_tf_journal.jsonl",
    "exists": true,
    "total_lines": 8812,
    "malformed_lines": 0
  },
  "fusion_journal": {
    "path": "logs/briefing_journal.jsonl",
    "exists": true,
    "total_lines": 595,
    "malformed_lines": 0
  },
  "tf_rows_in_window": 5028,
  "tf_rows_per_timeframe": {
    "15m": 1257,
    "1h": 1257,
    "4h": 1257,
    "1d": 1257
  },
  "tf_direction_counts": {
    "long": 202,
    "neutral": 4519,
    "short": 63,
    "standby": 244
  },
  "tf_last_ts": "2026-07-16T05:45:05.788268+00:00",
  "tf_last_age_open_minutes": 4.4,
  "fusion_rows_in_window": 70,
  "fusion_pit_rows_in_window": 6,
  "fusion_median_run_gap_minutes": 60.0
}
```

### outcome_maturation
```json
{
  "matured_scored": 1869,
  "matured_unresolved": 110,
  "immature": 110,
  "resolved_ratio": 0.9444,
  "outcome_counts": {
    "hit": 759,
    "miss": 797,
    "flat": 313
  },
  "unresolved_reasons": {
    "no_future_price_in_tolerance": 110
  },
  "scored_per_cell": {
    "USDJPY:1h": 146,
    "USDJPY:4h": 187,
    "EURUSD:1h": 164,
    "EURUSD:4h": 139,
    "GBPUSD:1h": 138,
    "GBPUSD:4h": 149,
    "USDJPY:15m": 173,
    "USDJPY:1d": 201,
    "EURUSD:15m": 194,
    "EURUSD:1d": 172,
    "GBPUSD:15m": 130,
    "GBPUSD:1d": 76
  }
}
```

### trade_outcome
```json
{
  "decision_outcomes_overall": {
    "evaluated": 290,
    "tradable": 266,
    "wins": 110,
    "losses": 154,
    "win_rate": 0.4135,
    "expectancy_r": -0.0386,
    "avg_mfe_r": 0.0838,
    "avg_mae_r": 0.1414,
    "tp1_rate": 0.0,
    "tp2_rate": 0.0,
    "sl_rate": 0.0,
    "sample_ok": true
  },
  "decision_outcomes_mtime": "2026-07-16T05:46:20.668645+00:00",
  "trade_outcome_monitor": null
}
```

### learning_update
```json
{
  "tf_learning_mtime": "2026-07-16T05:45:15.641918+00:00",
  "cells_with_scored_samples": 12,
  "cells_with_adjusted_weights": 9,
  "cells_with_conviction_damping": 2,
  "cells_with_condition_factors": 9,
  "cell_scored_samples": {
    "EURUSD|15m": 52,
    "USDJPY|15m": 57,
    "GBPUSD|15m": 38,
    "EURUSD|1h": 41,
    "USDJPY|1h": 40,
    "GBPUSD|1h": 39,
    "EURUSD|4h": 39,
    "USDJPY|4h": 44,
    "GBPUSD|4h": 37,
    "EURUSD|1d": 62,
    "USDJPY|1d": 67,
    "GBPUSD|1d": 22
  },
  "fusion_learning": {
    "generated_at": "2026-07-16T05:41:31.866860+00:00",
    "evaluated": 0,
    "tech_weight": 0.55,
    "news_weight": 0.45
  }
}
```

### decision_application
```json
{
  "rows_in_window": 5028,
  "rows_with_nondefault_tech_weight": 3974,
  "rows_with_net_expected_r": 0,
  "rows_with_target_policy": 0,
  "last_nondefault_weight_ts": "2026-07-16T05:45:05.788268+00:00",
  "learned_weight_cells": 9
}
```

### duplicate_detection
```json
{
  "journal_rows_in_window": 5028,
  "journal_duplicate_rows": 0,
  "price_rows_in_window": 4162,
  "price_duplicate_rows": 0,
  "err_log_duplicate_writer_hits": 1
}
```

### freshness
```json
{
  "monitor_timestamp": "2026-07-16T05:45:52.634413+00:00",
  "report_age_minutes": 3.7,
  "target_statuses": {
    "tf_price_snapshot": "ok",
    "tf_journal": "ok"
  }
}
```

### scanner_429
```json
{
  "all_time_scanner_error_lines": 1478,
  "recent_scanner_error_lines_tail200": 151,
  "recent_timeout_lines_tail200": 0,
  "collection_currently_healthy": true,
  "per_file": {
    "logs/launchd/snapshot.err.log": {
      "total_lines": 2321,
      "scanner_pattern_counts": {
        "Expecting value": 1478
      },
      "last_scanner_match_line": 2309
    },
    "logs/fx_integrated_briefing.log": {
      "total_lines": 918,
      "scanner_pattern_counts": {},
      "last_scanner_match_line": null
    },
    "logs/fx_fusion_capture.log": {
      "total_lines": 41,
      "scanner_pattern_counts": {},
      "last_scanner_match_line": null
    }
  }
}
```

### sample_sufficiency
```json
{
  "scored_samples_per_cell": {
    "USDJPY:1h": 146,
    "USDJPY:4h": 187,
    "EURUSD:1h": 164,
    "EURUSD:4h": 139,
    "GBPUSD:1h": 138,
    "GBPUSD:4h": 149,
    "USDJPY:15m": 173,
    "USDJPY:1d": 201,
    "EURUSD:15m": 194,
    "EURUSD:1d": 172,
    "GBPUSD:15m": 130,
    "GBPUSD:1d": 76
  },
  "cells_at_or_above_weight_min_20": 12,
  "cells_at_or_above_symbol_min_8": 12,
  "condition_cell_min": 12,
  "fusion_pit_directional_rows_total": 2,
  "ml_min_train_rows": 150
}
```

### blocking_reasons
```json
{
  "negative_net_expected_r_rows_in_window": 0,
  "decision_feedback": {
    "generated_at": "2026-07-16T05:45:05.788268+00:00",
    "cells_total": 7,
    "cells_dampened": 6,
    "cells_blocked": 0
  }
}
```

### launchd
```json
{
  "services": {
    "com.fx-codex.snapshot": {
      "loaded": true,
      "last_exit_code": 0,
      "runs": 57
    },
    "com.fx-codex.briefing": {
      "loaded": true,
      "last_exit_code": 5,
      "runs": 56
    },
    "com.fx-codex.health": {
      "loaded": true,
      "last_exit_code": 0,
      "runs": 57
    }
  }
}
```
