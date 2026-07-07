from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from fx_backtester.models import UnsupportedConversionError, price_distance_to_usd_per_unit

TRADE_QUALITY_COLUMNS = [
    "trade_id",
    "symbol",
    "strategy",
    "side",
    "direction",
    "entry_hour",
    "entry_time",
    "exit_time",
    "hold_minutes",
    "bar_count",
    "window_available",
    "pricing_supported",
    "data_issue",
    "entry_price",
    "exit_price",
    "stop_price",
    "take_profit_price",
    "planned_rr",
    "mfe_price",
    "mae_price",
    "mfe_usd",
    "mae_usd",
    "mfe_r",
    "mae_r",
    "r_multiple",
    "net_pnl",
    "tp_configured",
    "tp_touched",
    "sl_touched",
    "tp_hit",
    "sl_hit",
    "mfe_reached_1r",
    "mae_reached_1r",
    "capture_efficiency",
]


SEGMENT_COLUMNS = [
    "segment_type",
    "segment",
    "decision",
    "sample_status",
    "trade_count",
    "expectancy_r",
    "expectancy_ci_low",
    "expectancy_ci_high",
    "win_rate",
    "avg_mfe_r",
    "avg_mae_r",
    "tp_hit_rate",
    "stop_hit_rate",
    "reason",
]


@dataclass(frozen=True)
class TradeQualityConfig:
    min_trades: int = 30
    full_confidence_trades: int = 100
    min_segment_trades: int = 20
    expectancy_bootstrap_samples: int = 1_000
    expectancy_bootstrap_seed: int = 42
    expectancy_confidence: float = 0.95
    min_expectancy_r: float = 0.0
    min_tp_sl_score: float = 55.0
    max_unpriced_trade_pct: float = 0.05


def evaluate_trade_quality(
    trades: pd.DataFrame,
    price_data: dict[str, pd.DataFrame] | None,
    *,
    qa_report: pd.DataFrame | None = None,
    config: TradeQualityConfig | None = None,
    conversion_rates: dict[str, float] | None = None,
    data_load_error: str | None = None,
) -> dict[str, Any]:
    settings = config or TradeQualityConfig()
    rates = {str(key).upper(): float(value) for key, value in (conversion_rates or {}).items()}

    by_trade = trade_mfe_mae_frame(trades, price_data or {}, rates)
    expectancy = expectancy_summary(trades, settings)
    sample_guard = sample_guard_summary(len(trades), settings)
    mfe_mae = mfe_mae_summary(by_trade)
    tp_sl = tp_sl_score_summary(by_trade, expectancy, sample_guard, settings)
    data_quality = data_quality_monitor_summary(
        qa_report,
        by_trade,
        data_load_error=data_load_error,
        config=settings,
    )
    segments = segment_edge_summary(by_trade, settings)
    ai_decision = ai_trade_decision_summary(
        expectancy=expectancy,
        sample_guard=sample_guard,
        mfe_mae=mfe_mae,
        tp_sl=tp_sl,
        data_quality=data_quality,
        segments=segments,
    )

    return {
        "by_trade": by_trade,
        "segments": segments,
        "expectancy": expectancy,
        "sample_guard": sample_guard,
        "mfe_mae": mfe_mae,
        "tp_sl": tp_sl,
        "data_quality": data_quality,
        "ai_decision": ai_decision,
    }


def trade_mfe_mae_frame(
    trades: pd.DataFrame,
    price_data: dict[str, pd.DataFrame],
    conversion_rates: dict[str, float] | None = None,
) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=TRADE_QUALITY_COLUMNS)

    rates = conversion_rates or {}
    rows: list[dict[str, Any]] = []
    normalized_data = {
        str(symbol).upper(): frame.sort_index() for symbol, frame in price_data.items()
    }
    for trade_id, (_, trade) in enumerate(trades.reset_index(drop=True).iterrows(), start=1):
        rows.append(_trade_mfe_mae_row(trade_id, trade, normalized_data, rates))
    return pd.DataFrame(rows, columns=TRADE_QUALITY_COLUMNS)


def expectancy_summary(trades: pd.DataFrame, config: TradeQualityConfig) -> dict[str, Any]:
    r = _numeric_series(trades, "r_multiple")
    net = _numeric_series(trades, "net_pnl")
    n = int(len(r))
    if n == 0:
        return {
            "trade_count": 0,
            "expectancy_r": 0.0,
            "expectancy_usd": 0.0,
            "expectancy_ci_low": 0.0,
            "expectancy_ci_high": 0.0,
            "win_rate": 0.0,
            "average_win_r": 0.0,
            "average_loss_r": 0.0,
            "profit_factor_r": 0.0,
            "status": "blocked_sample",
            "passed": False,
            "reason": "No trades are available for expectancy estimation.",
        }

    ci_low, ci_high = _bootstrap_mean_interval(
        r.to_numpy(dtype=float),
        samples=config.expectancy_bootstrap_samples,
        seed=config.expectancy_bootstrap_seed,
        confidence=config.expectancy_confidence,
    )
    wins = r[r > 0]
    losses = r[r < 0]
    gross_win = float(wins.sum())
    gross_loss = abs(float(losses.sum()))
    expectancy_r = float(r.mean())
    if n < config.min_trades:
        status = "blocked_sample"
        passed = False
        reason = f"Sample size {n} is below the minimum {config.min_trades} trades."
    elif ci_low > config.min_expectancy_r:
        status = "positive_edge"
        passed = True
        reason = "Bootstrap lower bound is above the minimum expectancy threshold."
    elif expectancy_r > config.min_expectancy_r:
        status = "weak_positive"
        passed = False
        reason = "Mean expectancy is positive, but the confidence interval is not."
    else:
        status = "negative_or_zero"
        passed = False
        reason = "Mean expectancy is not above the minimum threshold."

    return {
        "trade_count": n,
        "expectancy_r": expectancy_r,
        "expectancy_usd": float(net.mean()) if not net.empty else 0.0,
        "expectancy_ci_low": ci_low,
        "expectancy_ci_high": ci_high,
        "win_rate": float((r > 0).mean()),
        "average_win_r": float(wins.mean()) if not wins.empty else 0.0,
        "average_loss_r": abs(float(losses.mean())) if not losses.empty else 0.0,
        "profit_factor_r": (
            float("inf")
            if gross_loss == 0 and gross_win > 0
            else (gross_win / gross_loss if gross_loss > 0 else 0.0)
        ),
        "status": status,
        "passed": passed,
        "reason": reason,
    }


def sample_guard_summary(trade_count: int, config: TradeQualityConfig) -> dict[str, Any]:
    min_trades = max(int(config.min_trades), 0)
    full_confidence = max(int(config.full_confidence_trades), min_trades)
    if trade_count < min_trades:
        status = "blocked"
        confidence_weight = 0.0
        reason = f"Only {trade_count} trades; minimum is {min_trades}."
    elif full_confidence == min_trades:
        status = "full_confidence"
        confidence_weight = 1.0
        reason = "Minimum sample threshold is satisfied."
    else:
        confidence_weight = min(
            1.0,
            max(0.0, (trade_count - min_trades) / (full_confidence - min_trades)),
        )
        status = "partial_confidence" if confidence_weight < 1.0 else "full_confidence"
        reason = f"Sample threshold is satisfied; confidence weight is {confidence_weight:.2f}."
    return {
        "sample_size": int(trade_count),
        "min_trades": min_trades,
        "full_confidence_trades": full_confidence,
        "confidence_weight": float(confidence_weight),
        "passed": bool(trade_count >= min_trades),
        "status": status,
        "reason": reason,
    }


def mfe_mae_summary(by_trade: pd.DataFrame) -> dict[str, Any]:
    if by_trade.empty:
        return {
            "evaluated_trade_count": 0,
            "priced_trade_count": 0,
            "avg_mfe_r": 0.0,
            "median_mfe_r": 0.0,
            "avg_mae_r": 0.0,
            "median_mae_r": 0.0,
            "mfe_to_mae_ratio": 0.0,
            "mfe_reached_1r_rate": 0.0,
            "mae_reached_1r_rate": 0.0,
            "avg_capture_efficiency": 0.0,
        }

    priced = by_trade[by_trade["pricing_supported"].astype(bool)].copy()
    mfe = _numeric_series(priced, "mfe_r")
    mae = _numeric_series(priced, "mae_r")
    avg_mfe = float(mfe.mean()) if not mfe.empty else 0.0
    avg_mae = float(mae.mean()) if not mae.empty else 0.0
    capture = _numeric_series(priced, "capture_efficiency")
    return {
        "evaluated_trade_count": int(len(by_trade)),
        "priced_trade_count": int(len(priced)),
        "avg_mfe_r": avg_mfe,
        "median_mfe_r": float(mfe.median()) if not mfe.empty else 0.0,
        "q75_mfe_r": float(mfe.quantile(0.75)) if not mfe.empty else 0.0,
        "avg_mae_r": avg_mae,
        "median_mae_r": float(mae.median()) if not mae.empty else 0.0,
        "q75_mae_r": float(mae.quantile(0.75)) if not mae.empty else 0.0,
        "mfe_to_mae_ratio": (
            avg_mfe / avg_mae if avg_mae > 0 else (float("inf") if avg_mfe > 0 else 0.0)
        ),
        "mfe_reached_1r_rate": float((mfe >= 1.0).mean()) if not mfe.empty else 0.0,
        "mae_reached_1r_rate": float((mae >= 1.0).mean()) if not mae.empty else 0.0,
        "avg_capture_efficiency": float(capture.mean()) if not capture.empty else 0.0,
    }


def tp_sl_score_summary(
    by_trade: pd.DataFrame,
    expectancy: dict[str, Any],
    sample_guard: dict[str, Any],
    config: TradeQualityConfig,
) -> dict[str, Any]:
    if by_trade.empty:
        return {
            "score": 0.0,
            "passed": False,
            "tp_configured_rate": 0.0,
            "tp_touch_rate": 0.0,
            "tp_hit_rate": 0.0,
            "stop_touch_rate": 0.0,
            "stop_hit_rate": 0.0,
            "components": {},
            "reason": "No trades are available for TP/SL scoring.",
        }

    priced = by_trade[by_trade["pricing_supported"].astype(bool)].copy()
    if priced.empty:
        return {
            "score": 0.0,
            "passed": False,
            "tp_configured_rate": 0.0,
            "tp_touch_rate": 0.0,
            "tp_hit_rate": 0.0,
            "stop_touch_rate": 0.0,
            "stop_hit_rate": 0.0,
            "components": {},
            "reason": "No trades could be priced for MFE/MAE scoring.",
        }

    mfe = _numeric_series(priced, "mfe_r")
    mae = _numeric_series(priced, "mae_r")
    avg_mfe = float(mfe.mean()) if not mfe.empty else 0.0
    avg_mae = float(mae.mean()) if not mae.empty else 0.0
    mfe_mae_ratio = avg_mfe / avg_mae if avg_mae > 0 else (2.0 if avg_mfe > 0 else 0.0)
    mae_1r_rate = float((mae >= 1.0).mean()) if not mae.empty else 0.0
    capture = _numeric_series(priced, "capture_efficiency")
    avg_capture = float(capture.mean()) if not capture.empty else 0.0
    tp_configured = priced["tp_configured"].astype(bool)
    tp_configured_count = int(tp_configured.sum())
    tp_hit_rate = (
        float(priced.loc[tp_configured, "tp_hit"].astype(bool).mean())
        if tp_configured_count
        else 0.0
    )
    tp_touch_rate = (
        float(priced.loc[tp_configured, "tp_touched"].astype(bool).mean())
        if tp_configured_count
        else 0.0
    )
    tp_efficiency = 0.5 if tp_configured_count == 0 else 0.7 * tp_hit_rate + 0.3 * tp_touch_rate
    stop_hit_rate = float(priced["sl_hit"].astype(bool).mean())
    stop_touch_rate = float(priced["sl_touched"].astype(bool).mean())

    components = {
        "mfe_mae_balance": 30.0 * min(max(mfe_mae_ratio / 2.0, 0.0), 1.0),
        "adverse_control": 25.0 * (1.0 - min(max(mae_1r_rate, 0.0), 1.0)),
        "tp_realization": 25.0 * min(max(tp_efficiency, 0.0), 1.0),
        "capture_efficiency": 20.0 * min(max(avg_capture / 0.6, 0.0), 1.0),
    }
    score = float(sum(components.values()))
    passed = bool(score >= config.min_tp_sl_score)
    reason = (
        "TP/SL behavior is acceptable."
        if passed
        else f"TP/SL score {score:.1f} is below {config.min_tp_sl_score:.1f}."
    )
    return {
        "score": score,
        "passed": passed,
        "minimum_score": float(config.min_tp_sl_score),
        "tp_configured_rate": float(tp_configured.mean()),
        "tp_touch_rate": tp_touch_rate,
        "tp_hit_rate": tp_hit_rate,
        "stop_touch_rate": stop_touch_rate,
        "stop_hit_rate": stop_hit_rate,
        "mfe_mae_ratio": mfe_mae_ratio,
        "mae_reached_1r_rate": mae_1r_rate,
        "avg_capture_efficiency": avg_capture,
        "expectancy_status": expectancy.get("status"),
        "sample_confidence_weight": float(sample_guard.get("confidence_weight", 0.0)),
        "components": components,
        "reason": reason,
    }


def data_quality_monitor_summary(
    qa_report: pd.DataFrame | None,
    by_trade: pd.DataFrame,
    *,
    data_load_error: str | None,
    config: TradeQualityConfig,
) -> dict[str, Any]:
    qa = qa_report if qa_report is not None else pd.DataFrame()
    qa_available = not qa.empty
    qa_passed = bool(qa["passed"].astype(bool).all()) if qa_available and "passed" in qa else False
    failed_symbols = (
        qa.loc[qa["passed"].astype(bool).eq(False), "symbol"].astype(str).tolist()
        if qa_available and {"passed", "symbol"}.issubset(qa.columns)
        else []
    )
    worst_missing_pct = _qa_float_max(qa, "missing_pct")
    duplicate_count = _qa_int_sum(qa, "duplicate_count")
    invalid_ohlc_count = _qa_int_sum(qa, "invalid_ohlc_count")
    null_ohlc_count = _qa_int_sum(qa, "null_ohlc_count")

    total_trades = int(len(by_trade))
    if total_trades:
        window_coverage = float(by_trade["window_available"].astype(bool).mean())
        pricing_coverage = float(by_trade["pricing_supported"].astype(bool).mean())
    else:
        window_coverage = 1.0
        pricing_coverage = 1.0
    unpriced_pct = 1.0 - pricing_coverage
    passed = (
        data_load_error is None
        and qa_passed
        and unpriced_pct <= config.max_unpriced_trade_pct
        and window_coverage >= 1.0 - config.max_unpriced_trade_pct
    )
    if passed:
        status = "pass"
    elif data_load_error is not None or not qa_available or not qa_passed:
        status = "fail"
    else:
        status = "degraded"

    return {
        "status": status,
        "passed": passed,
        "qa_available": qa_available,
        "qa_passed": qa_passed,
        "data_load_error": data_load_error,
        "failed_symbols": failed_symbols,
        "worst_missing_pct": worst_missing_pct,
        "duplicate_count": duplicate_count,
        "invalid_ohlc_count": invalid_ohlc_count,
        "null_ohlc_count": null_ohlc_count,
        "trade_count": total_trades,
        "trade_window_coverage": window_coverage,
        "pricing_coverage": pricing_coverage,
        "max_unpriced_trade_pct": float(config.max_unpriced_trade_pct),
    }


def segment_edge_summary(by_trade: pd.DataFrame, config: TradeQualityConfig) -> pd.DataFrame:
    if by_trade.empty:
        return pd.DataFrame(columns=SEGMENT_COLUMNS)

    rows: list[dict[str, Any]] = []
    for segment_type, column in (
        ("symbol", "symbol"),
        ("strategy", "strategy"),
        ("side", "side"),
        ("entry_hour", "entry_hour"),
    ):
        for segment, frame in by_trade.groupby(column, sort=True):
            rows.append(_segment_row(segment_type, str(segment), frame, config))
    return pd.DataFrame(rows, columns=SEGMENT_COLUMNS)


def ai_trade_decision_summary(
    *,
    expectancy: dict[str, Any],
    sample_guard: dict[str, Any],
    mfe_mae: dict[str, Any],
    tp_sl: dict[str, Any],
    data_quality: dict[str, Any],
    segments: pd.DataFrame,
) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    if not data_quality.get("passed"):
        blockers.append(
            {
                "code": "data_quality",
                "message": "Data quality or trade-window coverage is not sufficient.",
            }
        )
    if not sample_guard.get("passed"):
        blockers.append(
            {
                "code": "sample_guard",
                "message": str(sample_guard.get("reason", "Sample guard failed.")),
            }
        )
    if not expectancy.get("passed"):
        blockers.append(
            {
                "code": "expectancy",
                "message": str(expectancy.get("reason", "Expectancy gate failed.")),
            }
        )
    if not tp_sl.get("passed"):
        blockers.append(
            {
                "code": "tp_sl_score",
                "message": str(tp_sl.get("reason", "TP/SL score failed.")),
            }
        )

    actions = _improvement_actions(expectancy, sample_guard, mfe_mae, tp_sl, data_quality, segments)
    deployable = not blockers
    verdict = "positive_expectancy_candidate" if deployable else "blocked"
    return {
        "deployable": deployable,
        "verdict": verdict,
        "blockers": blockers,
        "expectancy_status": expectancy.get("status"),
        "tp_sl_score": tp_sl.get("score", 0.0),
        "sample_confidence_weight": sample_guard.get("confidence_weight", 0.0),
        "improvement_actions": actions,
    }


def _trade_mfe_mae_row(
    trade_id: int,
    trade: pd.Series,
    price_data: dict[str, pd.DataFrame],
    conversion_rates: dict[str, float],
) -> dict[str, Any]:
    symbol = str(trade.get("symbol", "")).upper()
    strategy = str(trade.get("strategy", ""))
    direction = _safe_int(trade.get("direction"), default=0)
    side = "long" if direction == 1 else ("short" if direction == -1 else "unknown")
    entry_time = pd.Timestamp(trade.get("entry_time"))
    exit_time = pd.Timestamp(trade.get("exit_time"))
    entry_price = _safe_float(trade.get("entry_price"))
    exit_price = _safe_float(trade.get("exit_price"))
    stop_price = _safe_float(trade.get("stop_price"))
    take_profit_price = _safe_float(trade.get("take_profit_price"))
    r_multiple = _safe_float(trade.get("r_multiple"))
    net_pnl = _safe_float(trade.get("net_pnl"))
    units = abs(_safe_float(trade.get("units")))
    initial_risk_usd = _safe_float(trade.get("initial_risk_usd"))
    exit_reason = str(trade.get("exit_reason", trade.get("reason", "")))

    base = {
        "trade_id": trade_id,
        "symbol": symbol,
        "strategy": strategy,
        "side": side,
        "direction": direction,
        "entry_hour": entry_time.hour if not pd.isna(entry_time) else -1,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "hold_minutes": _hold_minutes(entry_time, exit_time),
        "bar_count": 0,
        "window_available": False,
        "pricing_supported": False,
        "data_issue": "",
        "entry_price": entry_price,
        "exit_price": exit_price,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "planned_rr": _planned_rr(entry_price, stop_price, take_profit_price),
        "mfe_price": np.nan,
        "mae_price": np.nan,
        "mfe_usd": np.nan,
        "mae_usd": np.nan,
        "mfe_r": np.nan,
        "mae_r": np.nan,
        "r_multiple": r_multiple,
        "net_pnl": net_pnl,
        "tp_configured": bool(not np.isnan(take_profit_price)),
        "tp_touched": False,
        "sl_touched": False,
        "tp_hit": exit_reason == "take_profit",
        "sl_hit": exit_reason == "stop_loss",
        "mfe_reached_1r": False,
        "mae_reached_1r": False,
        "capture_efficiency": np.nan,
    }

    if pd.isna(entry_time) or pd.isna(exit_time) or exit_time < entry_time:
        base["data_issue"] = "invalid_trade_times"
        return base
    if direction not in (-1, 1) or np.isnan(entry_price):
        base["data_issue"] = "invalid_trade_price_or_direction"
        return base

    frame = price_data.get(symbol)
    if frame is None or frame.empty:
        base["data_issue"] = "missing_symbol_price_data"
        return base
    window = frame[(frame.index >= entry_time) & (frame.index <= exit_time)]
    if window.empty:
        base["data_issue"] = "missing_trade_window_bars"
        return base

    high = window["high"].astype(float)
    low = window["low"].astype(float)
    if direction == 1:
        mfe_price = max(float(high.max()) - entry_price, 0.0)
        mae_price = max(entry_price - float(low.min()), 0.0)
        tp_touched = bool(
            not np.isnan(take_profit_price) and float(high.max()) >= take_profit_price
        )
        sl_touched = bool(not np.isnan(stop_price) and float(low.min()) <= stop_price)
    else:
        mfe_price = max(entry_price - float(low.min()), 0.0)
        mae_price = max(float(high.max()) - entry_price, 0.0)
        tp_touched = bool(not np.isnan(take_profit_price) and float(low.min()) <= take_profit_price)
        sl_touched = bool(not np.isnan(stop_price) and float(high.max()) >= stop_price)

    base.update(
        {
            "bar_count": int(len(window)),
            "window_available": True,
            "mfe_price": mfe_price,
            "mae_price": mae_price,
            "tp_touched": tp_touched,
            "sl_touched": sl_touched,
        }
    )
    try:
        mfe_usd = (
            price_distance_to_usd_per_unit(symbol, mfe_price, entry_price, conversion_rates) * units
        )
        mae_usd = (
            price_distance_to_usd_per_unit(symbol, mae_price, entry_price, conversion_rates) * units
        )
    except UnsupportedConversionError as error:
        base["data_issue"] = str(error)
        return base

    mfe_r = mfe_usd / initial_risk_usd if initial_risk_usd > 0 else np.nan
    mae_r = mae_usd / initial_risk_usd if initial_risk_usd > 0 else np.nan
    capture = (
        min(max(r_multiple, 0.0) / mfe_r, 1.0)
        if not np.isnan(mfe_r) and mfe_r > 0 and not np.isnan(r_multiple)
        else 0.0
    )
    base.update(
        {
            "pricing_supported": bool(not np.isnan(mfe_r) and not np.isnan(mae_r)),
            "mfe_usd": mfe_usd,
            "mae_usd": mae_usd,
            "mfe_r": mfe_r,
            "mae_r": mae_r,
            "mfe_reached_1r": bool(not np.isnan(mfe_r) and mfe_r >= 1.0),
            "mae_reached_1r": bool(not np.isnan(mae_r) and mae_r >= 1.0),
            "capture_efficiency": capture,
        }
    )
    return base


def _segment_row(
    segment_type: str,
    segment: str,
    frame: pd.DataFrame,
    config: TradeQualityConfig,
) -> dict[str, Any]:
    r = _numeric_series(frame, "r_multiple")
    ci_low, ci_high = _bootstrap_mean_interval(
        r.to_numpy(dtype=float),
        samples=min(config.expectancy_bootstrap_samples, 500),
        seed=config.expectancy_bootstrap_seed,
        confidence=config.expectancy_confidence,
    )
    n = int(len(r))
    expectancy_r = float(r.mean()) if n else 0.0
    if n < config.min_segment_trades:
        decision = "collect_more"
        sample_status = "insufficient"
        reason = f"Needs at least {config.min_segment_trades} trades."
    elif ci_low > config.min_expectancy_r:
        decision = "keep_candidate"
        sample_status = "sufficient"
        reason = "Segment has positive confidence-adjusted expectancy."
    elif expectancy_r <= config.min_expectancy_r:
        decision = "block"
        sample_status = "sufficient"
        reason = "Segment expectancy is not positive."
    else:
        decision = "reduce_or_retest"
        sample_status = "sufficient"
        reason = "Segment mean is positive but confidence interval is weak."

    tp_configured = (
        frame["tp_configured"].astype(bool) if "tp_configured" in frame else pd.Series(dtype=bool)
    )
    return {
        "segment_type": segment_type,
        "segment": segment,
        "decision": decision,
        "sample_status": sample_status,
        "trade_count": n,
        "expectancy_r": expectancy_r,
        "expectancy_ci_low": ci_low,
        "expectancy_ci_high": ci_high,
        "win_rate": float((r > 0).mean()) if n else 0.0,
        "avg_mfe_r": _series_mean(frame, "mfe_r"),
        "avg_mae_r": _series_mean(frame, "mae_r"),
        "tp_hit_rate": (
            float(frame.loc[tp_configured, "tp_hit"].astype(bool).mean())
            if bool(tp_configured.any())
            else 0.0
        ),
        "stop_hit_rate": float(frame["sl_hit"].astype(bool).mean()) if not frame.empty else 0.0,
        "reason": reason,
    }


def _improvement_actions(
    expectancy: dict[str, Any],
    sample_guard: dict[str, Any],
    mfe_mae: dict[str, Any],
    tp_sl: dict[str, Any],
    data_quality: dict[str, Any],
    segments: pd.DataFrame,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not data_quality.get("passed"):
        actions.append(
            {
                "action": "fix_data_quality",
                "priority": "high",
                "reason": "Do not train or trade on failed QA or missing trade-window bars.",
            }
        )
    if not sample_guard.get("passed"):
        actions.append(
            {
                "action": "collect_more_samples",
                "priority": "high",
                "reason": sample_guard.get("reason"),
            }
        )
    if expectancy.get("status") in {"negative_or_zero", "weak_positive"}:
        actions.append(
            {
                "action": "block_or_retest_edge",
                "priority": "high",
                "reason": expectancy.get("reason"),
            }
        )
    if (
        float(mfe_mae.get("avg_mfe_r", 0.0)) >= 1.0
        and float(mfe_mae.get("avg_capture_efficiency", 0.0)) < 0.35
    ):
        actions.append(
            {
                "action": "improve_exit_capture",
                "priority": "medium",
                "reason": "Trades often move favorably but do not retain enough of MFE.",
            }
        )
    if float(mfe_mae.get("mae_reached_1r_rate", 0.0)) > 0.35:
        actions.append(
            {
                "action": "tighten_entry_filters_or_stop_model",
                "priority": "medium",
                "reason": "A large share of trades reaches at least 1R adverse excursion.",
            }
        )
    if float(tp_sl.get("tp_touch_rate", 0.0)) > float(tp_sl.get("tp_hit_rate", 0.0)) + 0.10:
        actions.append(
            {
                "action": "review_tp_execution_buffer",
                "priority": "medium",
                "reason": "Price touches TP materially more often than filled TP exits.",
            }
        )
    blocked_segments = (
        segments[segments["decision"].isin(["block", "reduce_or_retest"])]
        if not segments.empty and "decision" in segments
        else pd.DataFrame()
    )
    if not blocked_segments.empty:
        actions.append(
            {
                "action": "filter_weak_segments",
                "priority": "medium",
                "reason": "Some symbols, sides, hours, or strategies have weak segment expectancy.",
                "segments": blocked_segments[
                    ["segment_type", "segment", "decision", "trade_count", "expectancy_r"]
                ].to_dict(orient="records"),
            }
        )
    return actions


def _bootstrap_mean_interval(
    values: np.ndarray,
    *,
    samples: int,
    seed: int,
    confidence: float,
) -> tuple[float, float]:
    clean = values[np.isfinite(values)]
    if len(clean) == 0:
        return 0.0, 0.0
    if len(clean) == 1 or samples <= 0:
        mean = float(np.mean(clean))
        return mean, mean
    confidence = min(max(float(confidence), 0.50), 0.999)
    alpha = 1.0 - confidence
    rng = np.random.default_rng(seed)
    sampled = rng.choice(clean, size=(int(samples), len(clean)), replace=True)
    means = sampled.mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()


def _series_mean(frame: pd.DataFrame, column: str) -> float:
    values = _numeric_series(frame, column)
    return float(values.mean()) if not values.empty else 0.0


def _qa_float_max(frame: pd.DataFrame, column: str) -> float:
    values = _numeric_series(frame, column)
    return float(values.max()) if not values.empty else 0.0


def _qa_int_sum(frame: pd.DataFrame, column: str) -> int:
    values = _numeric_series(frame, column)
    return int(values.sum()) if not values.empty else 0


def _safe_float(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return numeric


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _planned_rr(entry_price: float, stop_price: float, take_profit_price: float) -> float:
    if np.isnan(entry_price) or np.isnan(stop_price) or np.isnan(take_profit_price):
        return float("nan")
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return float("nan")
    return abs(take_profit_price - entry_price) / stop_distance


def _hold_minutes(entry_time: pd.Timestamp, exit_time: pd.Timestamp) -> float:
    if pd.isna(entry_time) or pd.isna(exit_time) or exit_time < entry_time:
        return 0.0
    return float((exit_time - entry_time).total_seconds() / 60)
