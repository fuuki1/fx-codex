"""一元化された型付き設定。

全サービスが `from config import settings` で同じ設定を読む。env の検証を
ここで一度だけ行い、不正値は起動時に即落とす（ミッションクリティカルでは
「設定ミスのまま黙って動く」のが最悪なので fail-fast にする）。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# IB Gateway のポート（gnzsnz/ib-gateway イメージの規約）
IB_PORT_PAPER = 4002
IB_PORT_LIVE = 4001

# IB reqHistoricalData がサポートするバー間隔（秒 → barSizeSetting 文字列）。
# strategy.fetch_prices が取得本数を slow_window から逆算する際の基準になる。
# ここに無い間隔は設定バリデーションで弾く（黙って別間隔で動くのを防ぐ）。
IB_BAR_SIZE_STR = {
    5: "5 secs",
    10: "10 secs",
    15: "15 secs",
    30: "30 secs",
    60: "1 min",
    300: "5 mins",
    900: "15 mins",
    1800: "30 mins",
    3600: "1 hour",
}
_IB_BAR_SIZES = frozenset(IB_BAR_SIZE_STR)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 取引モード -------------------------------------------------------
    trading_mode: Literal["paper", "live"] = "paper"
    allow_live: bool = False

    # ---- IBKR -------------------------------------------------------------
    ib_host: str = "ib-gateway"
    ib_client_id: int = 11

    # ---- DB ---------------------------------------------------------------
    db_host: str = "timescaledb"
    db_port: int = 5432
    postgres_user: str = "trader"
    postgres_password: str = "trader"
    postgres_db: str = "trader"

    # ---- Redis ------------------------------------------------------------
    redis_host: str = "redis"
    redis_port: int = 6379

    # ---- Webhook セキュリティ --------------------------------------------
    # NoDecode: env のカンマ区切り文字列を JSON 解釈せず、下の validator に生のまま渡す
    tv_allowed_ips: Annotated[list[str], NoDecode] = Field(default_factory=list)
    webhook_secret: str = ""

    # ---- リスク上限 -------------------------------------------------------
    max_position_qty: float = 10_000
    max_daily_loss_jpy: float = 50_000
    max_orders_per_min: int = 10
    max_consecutive_errors: int = 5
    enforce_session: bool = True
    # 新規建てシグナルに stop_price / stop_distance を必須にする（安全側の既定 ON）。
    # 決済シグナルは "close": true で免除。
    require_stop_loss: bool = True
    # 取引を許可する銘柄（カンマ区切り、大文字化して照合）。
    # 空 = 制限なし（後方互換のため許容するが、本番では必ず設定すること）。
    symbol_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # シンボル毎の純建玉（ネットポジション）上限。建玉を「増やす」発注のみ制限し、
    # 減らす方向（決済）は常に通す。MAX_POSITION_QTY は 1 注文の上限、
    # こちらは累積の上限（同方向シグナル連打による積み上がりを止める）。
    max_net_position_qty: float = 10_000

    # ---- 自作戦略（strategy.py）------------------------------------------
    # 既定 OFF。明示的に有効化しない限り自動シグナルは出さない（安全側）。
    strategy_enabled: bool = False
    strategy_symbol: str = "USDJPY"
    strategy_asset: str = "fx"
    strategy_qty: float = 1000
    strategy_interval_sec: int = 15
    strategy_params_file: str = "strategy_params.json"
    # 価格バーの間隔（秒）。IB reqHistoricalData の barSizeSetting に対応。
    # slow_window が大きいほど必要な履歴が延びるため、取得本数は slow_window から
    # 逆算する（fetch_prices）。ここは 1 バーの時間軸だけを決める。
    # 対応値: 5/10/15/30 秒, 60(=1分)/300(5分)/900(15分)/1800(30分)/3600(1時間)。
    strategy_bar_size_sec: int = 5

    @field_validator("strategy_bar_size_sec")
    @classmethod
    def _validate_bar_size(cls, v: int) -> int:
        if v not in _IB_BAR_SIZES:
            raise ValueError(
                f"strategy_bar_size_sec={v} は IB がサポートするバー間隔でない。"
                f"許容値: {sorted(_IB_BAR_SIZES)}"
            )
        return v

    # ---- 通知 -------------------------------------------------------------
    discord_webhook_url: str = ""
    notify_throttle_sec: int = 300

    # ---- ログ -------------------------------------------------------------
    log_level: str = "INFO"

    @field_validator("tv_allowed_ips", mode="before")
    @classmethod
    def _split_ips(cls, v: object) -> object:
        """カンマ区切り文字列を list に変換（空要素は除去）。"""
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @field_validator("symbol_allowlist", mode="before")
    @classmethod
    def _split_symbols(cls, v: object) -> object:
        """カンマ区切り文字列を大文字の list に変換（空要素は除去）。"""
        if isinstance(v, str):
            return [p.strip().upper() for p in v.split(",") if p.strip()]
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ib_port(self) -> int:
        return IB_PORT_LIVE if self.trading_mode == "live" else IB_PORT_PAPER

    @computed_field  # type: ignore[prop-decorator]
    @property
    def live_enabled(self) -> bool:
        """実弾発注が許可されているか（二重ガード）。"""
        return self.trading_mode == "live" and self.allow_live

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_conninfo(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.postgres_db} "
            f"user={self.postgres_user} password={self.postgres_password}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# 利便性のためのシングルトン。テストでは get_settings.cache_clear() で差し替え可能。
settings = get_settings()
