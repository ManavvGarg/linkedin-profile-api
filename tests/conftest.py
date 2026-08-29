"""Test isolation.

Tests must never read the developer's real `.env`. Without this, a machine with
a live LINKEDIN_LI_AT configured produces different results from a clean
checkout or CI — which is exactly the kind of "passes locally, fails in CI"
divergence that erodes trust in the suite.

Pydantic-settings reads `.env` at Settings construction, so the file is
neutralised for the whole session rather than per-test.
"""

import os

import pytest

# Applied at import time: app.main constructs Settings at module import, which
# happens before any fixture can run.
os.environ["FETCH_MODE"] = "fixture"
os.environ.pop("LINKEDIN_LI_AT", None)
os.environ.pop("API_KEYS", None)


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch):
    """Point Settings at a non-existent env file, and clear inherited config."""
    from app.config import Settings, get_settings

    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}, raising=False
    )
    for key in ("LINKEDIN_LI_AT", "LINKEDIN_JSESSIONID", "API_KEYS", "PROXY_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FETCH_MODE", "fixture")

    get_settings.cache_clear()
    import app.main as main_module

    main_module._service = None
    yield
    get_settings.cache_clear()
    main_module._service = None
