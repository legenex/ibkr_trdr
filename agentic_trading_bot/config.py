"""Typed application configuration loaded from .env.

This module exposes a single validated `settings` object so the rest of the
system has one source of truth for connection details and, critically, the risk
limits. Validation is strict and loud: missing values, wrong types, and
out-of-range risk parameters raise a ValidationError at construction time rather
than failing silently somewhere deep inside the trading loop.

The default values here are conservative and paper-safe. Importing this module
never connects to a broker and never selects a live port on its own.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Directory containing this file. Relative paths in config are resolved against
# it so behavior does not depend on the process working directory.
PACKAGE_DIR = Path(__file__).resolve().parent

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseSettings):
    """All runtime configuration, validated.

    Field names map case-insensitively to the keys in .env (for example the
    field `max_daily_drawdown_pct` reads `MAX_DAILY_DRAWDOWN_PCT`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- IBKR connection ---
    ibkr_host: str = Field(default="127.0.0.1")
    ibkr_paper_port: int = Field(default=7497, ge=1, le=65535)
    ibkr_live_port: int = Field(default=7496, ge=1, le=65535)
    ibkr_gateway_paper_port: int = Field(default=4002, ge=1, le=65535)
    ibkr_gateway_live_port: int = Field(default=4001, ge=1, le=65535)
    ibkr_client_id: int = Field(default=1, ge=0)
    use_ib_gateway: bool = Field(default=False)

    # --- Live trading safety ---
    # Live requires BOTH this flag AND a typed runtime confirmation that must
    # equal live_confirmation_phrase. The phrase lives here; the runtime check
    # is enforced by the broker client in build stage 2.
    live_trading: bool = Field(default=False)
    live_confirmation_phrase: str = Field(default="I UNDERSTAND THIS IS REAL MONEY")

    # --- Secrets (optional until later build stages) ---
    anthropic_api_key: Optional[SecretStr] = Field(default=None)
    polygon_api_key: Optional[SecretStr] = Field(default=None)

    # --- Risk parameters (hard limits; ranges enforced) ---
    max_daily_drawdown_pct: float = Field(default=3.0, gt=0, le=100)
    max_weekly_drawdown_pct: float = Field(default=6.0, gt=0, le=100)
    # Capital risked per trade. Must be strictly between 0 and 5 percent.
    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=5)
    max_gross_exposure_pct: float = Field(default=100.0, gt=0, le=1000)
    max_single_name_weight_pct: float = Field(default=10.0, gt=0, le=100)
    max_correlated_cluster_exposure_pct: float = Field(default=25.0, gt=0, le=100)
    min_liquidity_adv: int = Field(default=1_000_000, gt=0)
    # Maximum order size as a percent of the symbol's average daily volume.
    max_adv_participation_pct: float = Field(default=5.0, gt=0, le=100)
    # Maximum gross-exposure-to-equity ratio. 1.0 means no leverage.
    max_leverage: float = Field(default=1.0, gt=0, le=10)
    # Absolute return correlation at or above which two names share a cluster.
    correlation_cluster_threshold: float = Field(default=0.7, ge=0, le=1)
    # Minimum overlapping return observations before a correlation is trusted.
    correlation_min_periods: int = Field(default=20, ge=2)

    # --- Operations / paths ---
    kill_switch_file: str = Field(default="journal/KILL_SWITCH")
    journal_dir: str = Field(default="journal")
    data_cache_dir: str = Field(default="journal/cache")
    log_level: str = Field(default="INFO")
    # Central seed for any randomness (model fitting, sampling) so runs reproduce.
    random_seed: int = Field(default=42, ge=0)
    # Per-run LLM budget for the learning loop. When exhausted, LLM steps are
    # skipped and logged (Agent SDK usage draws from a separate monthly credit).
    learning_token_budget: int = Field(default=200_000, ge=0)
    learning_cost_budget_usd: float = Field(default=5.0, ge=0)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
            )
        return upper

    @model_validator(mode="after")
    def _weekly_at_least_daily(self) -> "Settings":
        """A weekly drawdown limit below the daily limit is incoherent."""
        if self.max_weekly_drawdown_pct < self.max_daily_drawdown_pct:
            raise ValueError(
                "max_weekly_drawdown_pct must be >= max_daily_drawdown_pct "
                f"({self.max_weekly_drawdown_pct} < {self.max_daily_drawdown_pct})"
            )
        return self

    @model_validator(mode="after")
    def _live_requires_phrase(self) -> "Settings":
        """If live trading is enabled, the confirmation phrase must be set."""
        if self.live_trading and not self.live_confirmation_phrase.strip():
            raise ValueError(
                "live_trading=true requires a non-empty live_confirmation_phrase"
            )
        return self

    # --- Resolved paths (absolute, working-directory independent) ---
    def _resolve(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else (PACKAGE_DIR / path)

    @property
    def journal_path(self) -> Path:
        """Absolute path to the journal directory."""
        return self._resolve(self.journal_dir)

    @property
    def cache_path(self) -> Path:
        """Absolute path to the on-disk data cache directory."""
        return self._resolve(self.data_cache_dir)

    @property
    def logs_path(self) -> Path:
        """Absolute path to the per-run log directory."""
        return self.journal_path / "logs"

    @property
    def kill_switch_path(self) -> Path:
        """Absolute path to the kill-switch sentinel file."""
        return self._resolve(self.kill_switch_file)

    @property
    def audit_db_path(self) -> Path:
        """Absolute path to the append-only audit SQLite database."""
        return self.journal_path / "audit.db"

    def resolved_trading_port(self) -> int:
        """Return the port to connect to based on the live flag and transport.

        This selects the LIVE port only when live_trading is true. The typed
        runtime confirmation that the invariant also requires is enforced
        separately by the broker client before any live connection is made.
        """
        if self.live_trading:
            return self.ibkr_gateway_live_port if self.use_ib_gateway else self.ibkr_live_port
        return self.ibkr_gateway_paper_port if self.use_ib_gateway else self.ibkr_paper_port

    def ensure_dirs(self) -> None:
        """Create the journal, logs, and cache directories if they are missing."""
        for path in (self.journal_path, self.logs_path, self.cache_path):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object, built once and cached.

    Raises pydantic.ValidationError loudly if any value is missing, the wrong
    type, or out of range.
    """
    return Settings()


# Importing this module validates configuration immediately. With no .env
# present the conservative, paper-safe defaults apply and validation passes.
settings = get_settings()
