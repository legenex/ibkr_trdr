"""Tests for config validation.

Settings are constructed with `_env_file=None` so the tests are hermetic and do
not read a developer's local .env file.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import Settings


def make(**overrides) -> Settings:
    """Build a Settings object without reading any .env file."""
    return Settings(_env_file=None, **overrides)


def test_defaults_are_paper_safe() -> None:
    s = make()
    assert s.live_trading is False
    # With live off, the resolved port must be the paper TWS port.
    assert s.resolved_trading_port() == s.ibkr_paper_port == 7497


def test_live_flag_selects_live_port() -> None:
    s = make(live_trading=True)
    assert s.resolved_trading_port() == s.ibkr_live_port == 7496


def test_gateway_ports_selected_when_enabled() -> None:
    assert make(use_ib_gateway=True).resolved_trading_port() == 4002
    assert make(use_ib_gateway=True, live_trading=True).resolved_trading_port() == 4001


def test_risk_per_trade_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        make(risk_per_trade_pct=10)


def test_risk_per_trade_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make(risk_per_trade_pct=0)


def test_risk_per_trade_valid_edges_accepted() -> None:
    assert make(risk_per_trade_pct=5).risk_per_trade_pct == 5
    assert make(risk_per_trade_pct=0.01).risk_per_trade_pct == 0.01


def test_drawdown_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        make(max_daily_drawdown_pct=150)
    with pytest.raises(ValidationError):
        make(max_daily_drawdown_pct=0)


def test_weekly_drawdown_must_be_at_least_daily() -> None:
    with pytest.raises(ValidationError):
        make(max_daily_drawdown_pct=5, max_weekly_drawdown_pct=3)
    # Equal is allowed.
    s = make(max_daily_drawdown_pct=5, max_weekly_drawdown_pct=5)
    assert s.max_weekly_drawdown_pct == 5


def test_single_name_weight_range_enforced() -> None:
    with pytest.raises(ValidationError):
        make(max_single_name_weight_pct=200)


def test_min_liquidity_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make(min_liquidity_adv=0)


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        make(log_level="LOUD")


def test_log_level_is_normalized_upper() -> None:
    assert make(log_level="debug").log_level == "DEBUG"


def test_resolved_paths_are_absolute() -> None:
    s = make()
    assert s.audit_db_path.is_absolute()
    assert s.cache_path.is_absolute()
    assert s.kill_switch_path.is_absolute()
    assert s.logs_path.is_absolute()
