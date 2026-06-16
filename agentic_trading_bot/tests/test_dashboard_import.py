"""Import-smoke for the Streamlit dashboard.

Importing the module exercises every import and decorator without running the app
(main() is guarded behind __main__). Skipped if Streamlit is not installed.
"""
from __future__ import annotations

import pytest

streamlit = pytest.importorskip("streamlit")


def test_dashboard_module_imports():
    import ui.dashboard as dashboard

    assert hasattr(dashboard, "main")
    # Tab renderers are defined.
    for name in ("tab_dashboard", "tab_approvals", "tab_positions", "tab_holdout", "render_kill_switch"):
        assert hasattr(dashboard, name)
