"""Shared pytest fixtures: hermetic settings and a broker-client builder.

The fake ib_async IB lives in testsupport.fakes so test modules can import it
directly without depending on conftest import semantics.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from config import Settings
from testsupport.fakes import FakeIB
from utils.audit import AuditTrail


@pytest.fixture
def make_settings(tmp_path):
    """Build hermetic Settings rooted at the test's tmp_path."""

    def _make(**overrides) -> Settings:
        return Settings(
            _env_file=None,
            journal_dir=str(tmp_path / "journal"),
            data_cache_dir=str(tmp_path / "cache"),
            **overrides,
        )

    return _make


@pytest.fixture
def fake_ib() -> FakeIB:
    return FakeIB()


@pytest.fixture
def make_client(tmp_path, make_settings):
    """Return a builder that wires an IBKRClient to a FakeIB and a temp audit db."""
    from broker.ibkr_client import IBKRClient

    def _make(
        ib: Optional[FakeIB] = None,
        settings_kw: Optional[dict[str, Any]] = None,
        **client_kw: Any,
    ):
        ib = ib if ib is not None else FakeIB()
        s = make_settings(**(settings_kw or {}))
        audit = AuditTrail(tmp_path / "audit.db")
        client = IBKRClient(
            settings=s,
            audit=audit,
            ib_factory=lambda: ib,
            auto_reconnect=False,
            **client_kw,
        )
        return client, ib, audit

    return _make
