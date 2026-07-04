from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from fx_backtester.kelly import (
    KellyEstimate,
    VaREstimate,
    fractional_kelly_risk_pct,
    historical_var,
    kelly_fraction_from_r_multiples,
    var_breached,
)
from fx_backtester.models import instrument_for, notional_usd, price_distance_to_usd_per_unit


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.01
    risk_cap_pct: float = 0.01
    max_daily_loss_pct: float = 0.02
    max_weekly_loss_pct: float | None = None
    max_monthly_drawdown_pct: float | None = None
    monthly_profit_target_pct: float | None = None
    hard_drawdown_pct: float | None = None
    min_stop_pips: float = 5.0
    max_leverage: float = 10.0
    max_currency_exposure_pct: float | None = None
    max_position_units: float | None = None
    allow_fractional_units: bool = False
    # --- フラクショナル・ケリー(既定OFF。ON時のみ実現R倍数から動的サイジング) ---
    use_fractional_kelly: bool = False
    kelly_fraction: float = 0.25  # 0.25=クォーター, 0.5=ハーフ(レポート推奨帯)
    kelly_min_trades: int = 50  # これ未満は固定フラクショナルへフォールバック
    kelly_full_confidence_trades: int = 100  # ここで完全にケリーへ移行
    kelly_max_risk_pct: float = 0.02  # ケリー採用時の1トレードリスク上限(安全弁)
    # --- VaR(別枠監視。既定OFF。方向モデルと独立に実現分布から計算) ---
    var_limit_pct: float | None = None  # 1期間VaRがこれを超えたら新規建て停止
    var_confidence: float = 0.95
    var_min_samples: int = 30


class RiskManager:
    """Risk layer: position sizing and daily stop state."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self._current_day: Any = None
        self._current_week: Any = None
        self._current_month: Any = None
        self._day_start_equity: float | None = None
        self._week_start_equity: float | None = None
        self._month_start_equity: float | None = None
        self._month_peak_equity: float | None = None
        self._high_watermark_equity: float | None = None
        self._daily_locked = False
        self._weekly_locked = False
        self._monthly_locked = False
        self._monthly_profit_locked = False
        self._hard_locked = False
        # ケリー/VaR の現在推定(サイジングとゲートに使う。既定は固定フラクショナル)
        self._effective_risk_pct: float = self.config.risk_per_trade_pct
        self._kelly_estimate: KellyEstimate | None = None
        self._var_estimate: VaREstimate | None = None
        self._var_locked = False

    @property
    def daily_locked(self) -> bool:
        return self._daily_locked

    @property
    def weekly_locked(self) -> bool:
        return self._weekly_locked

    @property
    def monthly_locked(self) -> bool:
        return self._monthly_locked

    @property
    def monthly_profit_locked(self) -> bool:
        return self._monthly_profit_locked

    @property
    def hard_locked(self) -> bool:
        return self._hard_locked

    @property
    def var_locked(self) -> bool:
        return self._var_locked

    @property
    def risk_locked(self) -> bool:
        return (
            self._daily_locked
            or self._weekly_locked
            or self._monthly_locked
            or self._monthly_profit_locked
            or self._hard_locked
            or self._var_locked
        )

    @property
    def effective_risk_pct(self) -> float:
        """現在の1トレードあたりリスク%(ケリーONなら動的、OFFなら固定値)。"""
        return self._effective_risk_pct

    @property
    def kelly_estimate(self) -> KellyEstimate | None:
        return self._kelly_estimate

    @property
    def var_estimate(self) -> VaREstimate | None:
        return self._var_estimate

    def reset(self) -> None:
        self._current_day = None
        self._current_week = None
        self._current_month = None
        self._day_start_equity = None
        self._week_start_equity = None
        self._month_start_equity = None
        self._month_peak_equity = None
        self._high_watermark_equity = None
        self._daily_locked = False
        self._weekly_locked = False
        self._monthly_locked = False
        self._monthly_profit_locked = False
        self._hard_locked = False
        self._effective_risk_pct = self.config.risk_per_trade_pct
        self._kelly_estimate = None
        self._var_estimate = None
        self._var_locked = False

    def update_risk_budget(self, realized_r_multiples: Sequence[float]) -> float:
        """実現R倍数列からケリーで1トレードリスク%を更新し、採用値を返す。

        use_fractional_kelly=False のときは何もせず固定値(risk_per_trade_pct)を返す。
        ONのときは kelly_fraction_from_r_multiples → fractional_kelly_risk_pct で
        動的に決め、_effective_risk_pct に反映する。engine はトレードが1件確定する
        たびにこれを呼ぶ想定(まだ標本が薄い間は自動でフォールバックする)。
        """
        if not self.config.use_fractional_kelly:
            self._effective_risk_pct = self.config.risk_per_trade_pct
            return self._effective_risk_pct
        estimate = kelly_fraction_from_r_multiples(
            realized_r_multiples, min_trades=self.config.kelly_min_trades
        )
        self._kelly_estimate = estimate
        risk_pct, _note = fractional_kelly_risk_pct(
            estimate,
            baseline_pct=self.config.risk_per_trade_pct,
            fraction=self.config.kelly_fraction,
            max_risk_pct=self.config.kelly_max_risk_pct,
            full_confidence_trades=self.config.kelly_full_confidence_trades,
        )
        self._effective_risk_pct = risk_pct
        return risk_pct

    def update_var(self, equity_returns: Sequence[float]) -> VaREstimate:
        """実現equityリターン分布から別枠VaRを更新し、上限超過ならロックする。

        var_limit_pct が None なら監視のみ(ロックしない)。方向モデルとは独立に、
        実現リターンそのものからテールを測る(レポートの「VaR別枠」)。
        """
        estimate = historical_var(
            equity_returns,
            confidence=self.config.var_confidence,
            min_samples=self.config.var_min_samples,
        )
        self._var_estimate = estimate
        if self.config.var_limit_pct is not None:
            self._var_locked = var_breached(estimate, self.config.var_limit_pct)
        return estimate

    def on_bar(self, timestamp: Any, equity: float) -> None:
        normalized = pd.Timestamp(timestamp)
        day = normalized.date()
        if day != self._current_day:
            self._current_day = day
            self._day_start_equity = equity
            self._daily_locked = False

        iso = normalized.isocalendar()
        week = (int(iso.year), int(iso.week))
        if week != self._current_week:
            self._current_week = week
            self._week_start_equity = equity
            self._weekly_locked = False

        month = (normalized.year, normalized.month)
        if month != self._current_month:
            self._current_month = month
            self._month_start_equity = equity
            self._month_peak_equity = equity
            self._monthly_locked = False
            self._monthly_profit_locked = False

        if self._month_peak_equity is None or equity > self._month_peak_equity:
            self._month_peak_equity = equity
        if self._high_watermark_equity is None or equity > self._high_watermark_equity:
            self._high_watermark_equity = equity

    def check_daily_loss(self, timestamp: Any, equity: float) -> bool:
        self.on_bar(timestamp, equity)
        if self._day_start_equity is None:
            return False
        threshold = self._day_start_equity * (1 - self.config.max_daily_loss_pct)
        if equity <= threshold:
            self._daily_locked = True
        return self._daily_locked

    def check_portfolio_stops(self, timestamp: Any, equity: float) -> list[str]:
        self.on_bar(timestamp, equity)
        reasons: list[str] = []

        if (
            self.config.max_weekly_loss_pct is not None
            and self._week_start_equity is not None
            and equity <= self._week_start_equity * (1 - self.config.max_weekly_loss_pct)
        ):
            self._weekly_locked = True
            reasons.append("weekly_loss_stop")

        if (
            self.config.max_monthly_drawdown_pct is not None
            and self._month_peak_equity is not None
            and equity <= self._month_peak_equity * (1 - self.config.max_monthly_drawdown_pct)
        ):
            self._monthly_locked = True
            reasons.append("monthly_drawdown_stop")

        if (
            self.config.monthly_profit_target_pct is not None
            and self._month_start_equity is not None
            and equity >= self._month_start_equity * (1 + self.config.monthly_profit_target_pct)
        ):
            self._monthly_profit_locked = True
            reasons.append("monthly_profit_target")

        if (
            self.config.hard_drawdown_pct is not None
            and self._high_watermark_equity is not None
            and equity <= self._high_watermark_equity * (1 - self.config.hard_drawdown_pct)
        ):
            self._hard_locked = True
            reasons.append("hard_drawdown_stop")

        return reasons

    def can_open(self, timestamp: Any, equity: float, no_trade_window: bool) -> bool:
        self.on_bar(timestamp, equity)
        if no_trade_window:
            return False
        if self.risk_locked:
            return False
        return equity > 0

    def position_size(
        self,
        symbol: str,
        equity: float,
        entry_price: float,
        stop_distance: float,
        extra_risk_per_unit_usd: float = 0.0,
        extra_risk_usd: float = 0.0,
        conversion_rates: dict[str, float] | None = None,
    ) -> tuple[float, float, float]:
        """Return units, adjusted stop distance, and initial risk in USD.

        The per-trade risk is capped by RiskConfig.risk_cap_pct.
        extra_risk_per_unit_usd is used for estimated round-trip spread/slippage/fees.
        extra_risk_usd is used for fixed or minimum fees that do not scale with size.
        """
        inst = instrument_for(symbol)
        min_stop_distance = inst.pip_size * self.config.min_stop_pips
        adjusted_stop_distance = max(float(stop_distance), min_stop_distance)
        price_risk_per_unit = price_distance_to_usd_per_unit(
            symbol,
            adjusted_stop_distance,
            entry_price,
            conversion_rates,
        )
        total_risk_per_unit = price_risk_per_unit + max(extra_risk_per_unit_usd, 0.0)
        if total_risk_per_unit <= 0:
            return 0.0, adjusted_stop_distance, 0.0

        fixed_risk = max(float(extra_risk_usd), 0.0)
        # ケリーONなら _effective_risk_pct(動的)、OFFなら risk_per_trade_pct(固定)。
        # ハードキャップは、ケリーONのときは kelly_max_risk_pct(ケリー用の安全上限)、
        # OFFのときは従来どおり risk_cap_pct。これで固定運用の1%キャップは不変のまま、
        # ケリー運用だけが上限を kelly_max_risk_pct まで許容する。
        if self.config.use_fractional_kelly:
            hard_cap = self.config.kelly_max_risk_pct
            effective_pct = min(self._effective_risk_pct, hard_cap)
        else:
            effective_pct = min(self.config.risk_per_trade_pct, self.config.risk_cap_pct)
        risk_budget = equity * effective_pct
        if risk_budget <= fixed_risk:
            return 0.0, adjusted_stop_distance, 0.0
        units = (risk_budget - fixed_risk) / total_risk_per_unit

        notional_per_unit = notional_usd(symbol, 1.0, entry_price, conversion_rates)
        max_units_by_leverage = equity * self.config.max_leverage / notional_per_unit
        units = min(units, max_units_by_leverage)
        if self.config.max_position_units is not None:
            units = min(units, self.config.max_position_units)
        if not self.config.allow_fractional_units:
            units = float(int(units))
        if units <= 0:
            return 0.0, adjusted_stop_distance, 0.0

        initial_risk = units * total_risk_per_unit + fixed_risk
        return max(units, 0.0), adjusted_stop_distance, initial_risk
