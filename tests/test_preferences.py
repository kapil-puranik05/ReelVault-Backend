"""Faster Search is a paid-plan feature, enforced by the backend: the switch can't be turned on by a free user, and the
value the app acts on (`effective`) is computed from the subscription, never trusted from the client."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.storage import preference_store
from tests.test_subscription import USER, env  # noqa: F401  (env is a fixture)


@pytest.fixture
def prefs(env, monkeypatch):  # noqa: F811
    db, _, client = env
    stored: dict[str, bool] = {}
    monkeypatch.setattr(preference_store, "get_faster_search", lambda uid: stored.get(uid, True))
    monkeypatch.setattr(preference_store, "set_faster_search", lambda uid, on: stored.__setitem__(uid, on))
    return db, client, stored


def make_paid(db, days=20):
    future = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    db.set_trial(USER, days_from_now=-30, status="ACTIVE", plan="PREMIUM",
                 subscription_start_date=future, subscription_end_date=future)


def test_free_trial_user_is_not_entitled_and_cannot_turn_it_on(prefs):
    db, client, stored = prefs
    db.set_trial(USER, days_from_now=3)
    assert client.get("/api/user/preferences").json() == {"fasterSearch": {"entitled": False, "enabled": True, "effective": False}}
    res = client.put("/api/user/preferences", json={"fasterSearch": True})
    assert res.status_code == 403 and res.json()["code"] == "PREMIUM_REQUIRED" and "error" in res.json()
    assert stored == {}  # nothing was saved


def test_free_user_may_still_switch_it_off(prefs):
    db, client, stored = prefs
    db.set_trial(USER, days_from_now=3)
    assert client.put("/api/user/preferences", json={"fasterSearch": False}).status_code == 200
    assert stored == {USER: False}


def test_paid_user_can_switch_it_on_and_off(prefs):
    db, client, stored = prefs
    make_paid(db)
    assert client.get("/api/user/preferences").json()["fasterSearch"] == {"entitled": True, "enabled": True, "effective": True}
    off = client.put("/api/user/preferences", json={"fasterSearch": False}).json()["fasterSearch"]
    assert off == {"entitled": True, "enabled": False, "effective": False}
    on = client.put("/api/user/preferences", json={"fasterSearch": True}).json()["fasterSearch"]
    assert on["effective"] is True and stored == {USER: True}


def test_lapsed_subscription_turns_it_off_even_though_the_switch_was_left_on(prefs):
    db, client, stored = prefs
    stored[USER] = True
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.set_trial(USER, days_from_now=-30, status="CANCELLED", plan="PREMIUM",
                 subscription_start_date=past, subscription_end_date=past)
    assert client.get("/api/user/preferences").json()["fasterSearch"] == {"entitled": False, "enabled": True, "effective": False}
    assert client.put("/api/user/preferences", json={"fasterSearch": True}).status_code == 403


def test_cancelled_but_still_paid_up_keeps_the_feature_until_the_period_ends(prefs):
    db, client, _ = prefs
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    db.set_trial(USER, days_from_now=-30, status="CANCELLED", plan="PREMIUM",
                 subscription_start_date=future, subscription_end_date=future)
    assert client.get("/api/user/preferences").json()["fasterSearch"]["effective"] is True


def test_brand_new_user_without_any_subscription_row_is_not_entitled(prefs):
    _, client, _ = prefs
    assert client.get("/api/user/preferences").json()["fasterSearch"]["entitled"] is False


def test_client_supplied_hints_cannot_grant_it(prefs):
    db, client, _ = prefs
    db.set_trial(USER, days_from_now=3)
    res = client.put("/api/user/preferences?premium=true", json={"fasterSearch": True, "isPremium": True, "entitled": True},
                     headers={"X-Premium": "true"})
    assert res.status_code == 403


def test_subscription_state_publishes_the_entitlement_for_each_plan(prefs):
    db, client, _ = prefs
    db.set_trial(USER, days_from_now=3)
    assert client.get("/api/subscription").json()["entitlements"] == {"fasterSearch": False}
    make_paid(db)
    assert client.get("/api/subscription").json()["entitlements"] == {"fasterSearch": True}
    db.set_trial(USER, days_from_now=-1)  # trial over, never paid
    assert client.get("/api/subscription").json()["entitlements"] == {"fasterSearch": False}


def test_malformed_body_is_rejected(prefs):
    _, client, _ = prefs
    assert client.put("/api/user/preferences", json={"fasterSearch": "maybe"}).status_code == 400
    assert client.put("/api/user/preferences", json={}).status_code == 400
