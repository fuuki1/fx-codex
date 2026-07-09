"""Phase 0: パッケージが import でき、CLI骨格が組み上がることを確認する。"""

from __future__ import annotations

import pytest

import dukascopy_cftc_model as dcm
from dukascopy_cftc_model.cli import build_parser
from dukascopy_cftc_model.config import (
    DEFAULT_ALPHA_GRID,
    PipelineConfig,
    WalkForwardConfig,
)


def test_package_exposes_version_and_configs() -> None:
    assert dcm.__version__
    cfg = PipelineConfig()
    assert cfg.data.symbol == "EURUSD"
    assert cfg.labels.horizon == 24
    assert list(cfg.walk_forward.alpha_grid) == list(DEFAULT_ALPHA_GRID)


def test_with_symbol_is_immutable_copy() -> None:
    cfg = PipelineConfig()
    other = cfg.with_symbol("USDJPY")
    assert other.data.symbol == "USDJPY"
    assert cfg.data.symbol == "EURUSD"  # 元は不変


def test_walk_forward_effective_step_defaults_to_test_bars() -> None:
    wf = WalkForwardConfig(test_bars=300, step_bars=None)
    assert wf.effective_step() == 300
    assert WalkForwardConfig(test_bars=300, step_bars=120).effective_step() == 120


def test_cli_parser_has_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["fetch", "--symbol", "USDJPY"])
    assert args.command == "fetch"
    assert args.symbol == "USDJPY"


def test_cli_requires_subcommand() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_cli_run_parses_full_args() -> None:
    # run サブコマンドが期間/足/ホライズン/offlineを受け取れること(実行はしない)。
    parser = build_parser()
    args = parser.parse_args(
        ["run", "--symbol", "USDJPY", "--horizon", "12", "--offline", "--timeframe", "H4"]
    )
    assert args.command == "run"
    assert args.symbol == "USDJPY"
    assert args.horizon == 12
    assert args.offline is True
    assert args.timeframe == "H4"
