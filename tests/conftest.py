"""Safety net: no test may reach the real Supabase project (the backend's .env holds real credentials).

Every storage module imports its own reference to `get_client`, so each one is replaced. A test that forgets to fake
a storage call now fails loudly instead of quietly querying the live database."""

from __future__ import annotations

import importlib

import pytest

_MODULES = [
    "pipeline.storage.supabase_store", "pipeline.storage.job_store", "pipeline.storage.subscription_store",
    "pipeline.storage.payment_store", "pipeline.storage.help_store", "pipeline.storage.preference_store",
    "pipeline.storage.push_store", "pipeline.storage.account_store", "pipeline.storage.admin_store",
]


@pytest.fixture(autouse=True)
def _never_touch_the_real_database(monkeypatch):
    attempts: list[str] = []

    def forbidden():
        attempts.append("get_client")
        raise AssertionError("A test tried to use the real Supabase client; fake the storage call instead.")

    for name in _MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "get_client"):
            monkeypatch.setattr(module, "get_client", forbidden)
    yield
    # Code under test may catch the error above (the job runner does, on purpose), so check afterwards as well.
    assert not attempts, f"A test reached for the real Supabase client {len(attempts)} time(s)."
