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


def _hhmm_to_min(s: str) -> int:
    """"HH:MM" を 0..1439 の分へ。不正値は fail-fast（設定ミスを起動時に落とす）。"""
    h, _, m = s.strip().partition(":")
    minutes = int(h) * 60 + int(m or 0)
    if not 0 <= minutes <= 1439:
        raise ValueError(f"invalid HH:MM time: {s!r}")
    return minutes


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
    # 信頼するリバースプロキシ段数。ngrok 経由は 1（= X-Forwarded-For の右端が実クライアント）。
    # 0 にすると XFF を無視し TCP ピア IP を使う（プロキシ無しの直接公開時）。
    # 右端を採用することで、クライアントが偽の XFF を足して IP 検証を迂回するのを防ぐ。
    tv_trusted_proxy_hops: int = 1
    # 受信ボディの最大バイト数（これを超える POST は 413 で拒否。安価な DoS 対策）。
    max_webhook_body_bytes: int = 65_536
    # シグナルの最大許容齢（秒）。alert に時刻（{{timenow}}）が含まれる場合のみ有効。
    # これより古い／未来すぎるシグナルは 409 で拒否（リプレイ・遅延配信の発注を防ぐ）。
    # 0 で無効。時刻フィールドが無いシグナルは受信時刻扱いなので影響しない。
    max_signal_age_sec: int = 120

    # ---- リスク上限 -------------------------------------------------------
    max_position_qty: float = 10_000
    max_daily_loss_jpy: float = 50_000
    max_orders_per_min: int = 10
    max_consecutive_errors: int = 5
    enforce_session: bool = True

    # ---- プロ級リスクエンジン（risk_engine.py）---------------------------
    # サイズは確信ではなくストップ距離と口座リスクで決める（Kovner）。既定 OFF＝
    # 明示有効化するまで qty はシグナルのまま（後方互換）。有効化前に account_equity
    # と risk_value_per_point を必ず実値に合わせること。
    risk_sizing_enabled: bool = False
    account_equity: float = 1_000_000.0      # 口座残高（口座通貨。サイジングの基準）
    risk_per_trade_pct: float = 0.5          # 1 取引で許容する口座割合（%）
    require_stop_for_sizing: bool = False    # True で stop 無しシグナルを却下
    lot_step: float = 1000.0                 # 発注ロットの最小刻み（切り捨て）
    min_lot: float = 1000.0                  # これ未満になるサイズは発注しない
    # 価格 1.0 動いたときの「1 単位あたり損益（口座通貨）」。JPY 建てペア×JPY 口座は 1.0。
    # 例: "USDJPY=1.0,EURJPY=1.0"。未指定の銘柄は 1.0 とみなす。
    risk_value_per_point: Annotated[dict[str, float], NoDecode] = Field(default_factory=dict)
    # 週次損失上限（0 で無効）。超過で Kill switch（翌週まで新規停止／手動解除）。
    max_weekly_loss_jpy: float = 0.0
    # 連敗スロットル（Lipschutz: 連敗時はサイズ縮小→停止）
    loss_streak_reduce_at: int = 3           # この連敗数でサイズ縮小
    loss_streak_reduce_factor: float = 0.5   # 縮小係数（0.5＝半減）
    loss_streak_halt_at: int = 5             # この連敗数で新規停止（0 で無効）
    recent_trades_window: int = 50           # 連敗判定に見る直近トレード数
    # 集中・相関（Lipschutz: 高相関は 1 つの巨大ポジション）
    max_concurrent_positions: int = 3        # 同時に持てる別銘柄数（0 で無効）
    max_currency_exposure: float = 0.0       # 1 通貨あたり純エクスポージャ上限（0 で無効）
    # 重要指標ブラックアウト窓の定義ファイル（無ければ無効）
    risk_blackout_file: str = "risk_calendar.json"

    # ---- プロ級リスクエンジン 第2層（非対称性・規律・DD・薄商い）-----------
    # 非対称性（相場観より「外れても小さく当たれば大きい」）。報酬/リスク比の下限（0=無効）。
    # シグナルに利確距離（tp_distance / take_profit / target）がある時のみ評価。
    min_reward_risk: float = 0.0
    # True で利確目標の無いシグナルを却下（R:R を必須化）。
    require_target_for_rr: bool = False
    # True で「根拠（reason/comment）」の無いシグナルを却下（理由を文章化できないなら入らない）。
    require_reason: bool = False
    # 実現損益の高値からのドローダウンが口座の何%でKillSwitch（0=無効）。Report2 §G の強制停止。
    # 基準は「全期間の累計実現損益」の高値（HWM）。集計期間の窓は設けない（窓を切ると古い利益が
    # 期間外へ抜けて cum が減り、損失が無くても DD が膨らむ＝誤発火するため）。
    max_drawdown_pct: float = 0.0
    # 薄商い時間帯（UTC, "HH:MM-HH:MM" カンマ区切り）。新規を抑止。例: FX ロールオーバ "20:55-22:05"。
    thin_liquidity_windows: Annotated[list[tuple[int, int]], NoDecode] = Field(default_factory=list)

    # ---- 自作戦略（strategy.py）------------------------------------------
    # 既定 OFF。明示的に有効化しない限り自動シグナルは出さない（安全側）。
    strategy_enabled: bool = False
    strategy_symbol: str = "USDJPY"
    strategy_asset: str = "fx"
    strategy_qty: float = 1000
    strategy_interval_sec: int = 15
    strategy_params_file: str = "strategy_params.json"

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

    @field_validator("risk_value_per_point", mode="before")
    @classmethod
    def _parse_value_per_point(cls, v: object) -> object:
        """"USDJPY=1.0,EURJPY=1.0" 形式を {symbol: float} に変換。

        不正な要素は fail-fast で落とす（設定ミスのまま黙って動かさない）。
        """
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            out: dict[str, float] = {}
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                key, _, val = part.partition("=")
                out[key.strip().upper()] = float(val)
            return out
        return v

    @field_validator("thin_liquidity_windows", mode="before")
    @classmethod
    def _parse_thin_windows(cls, v: object) -> object:
        """"HH:MM-HH:MM,HH:MM-HH:MM" を [(start_min, end_min), ...]（UTC 分）へ変換。

        日跨ぎ（例 23:30-00:30）は start>end として表現し、評価側でラップ処理する。
        """
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            out: list[tuple[int, int]] = []
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                lo, _, hi = part.partition("-")
                out.append((_hhmm_to_min(lo), _hhmm_to_min(hi)))
            return out
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
