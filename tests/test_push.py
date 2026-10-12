"""Push delivery to Expo's service (HTTP and storage faked) and the token endpoints."""

from __future__ import annotations

import pytest

from api import push
from pipeline.config import settings
from pipeline.storage import push_store
from tests.test_subscription import OTHER, USER, env  # noqa: F401  (env is a fixture)

TOKEN = "ExponentPushToken[abcdefghijklmnop]"
TOKEN2 = "ExponentPushToken[zyxwvutsrqponmlk]"


class FakeTokens:
    def __init__(self):
        self.rows: dict[str, str] = {}  # token -> user

    def install(self, monkeypatch):
        db = self
        monkeypatch.setattr(push_store, "upsert", lambda t, u, p: db.rows.__setitem__(t, u))
        monkeypatch.setattr(push_store, "delete_for_user", lambda u, t: db.rows.pop(t, None) if db.rows.get(t) == u else None)
        monkeypatch.setattr(push_store, "delete_token", lambda t: db.rows.pop(t, None))
        monkeypatch.setattr(push_store, "list_for_user", lambda u: [{"token": t} for t, o in db.rows.items() if o == u])


class FakeExpo:
    def __init__(self):
        self.calls: list[dict] = []
        self.status_code = 200
        self.tickets = None

    def install(self, monkeypatch):
        expo = self

        def post(url, json=None, headers=None, timeout=None):
            expo.calls.append({"url": url, "messages": json, "headers": headers})
            tickets = expo.tickets if expo.tickets is not None else [{"status": "ok"} for _ in json]
            return type("R", (), {"status_code": expo.status_code, "json": lambda self: {"data": tickets}})()

        monkeypatch.setattr(push.httpx, "post", post)


@pytest.fixture
def tokens(monkeypatch):
    t, e = FakeTokens(), FakeExpo()
    t.install(monkeypatch)
    e.install(monkeypatch)
    return t, e


# ── Sending ──────────────────────────────────────────────────────────────────

def test_ready_notification_goes_to_the_users_devices_with_the_reel_to_open(tokens):
    db, expo = tokens
    db.rows.update({TOKEN: USER, TOKEN2: USER, "ExponentPushToken[someoneelse12345]": OTHER})
    assert push.notify_reel_ready(USER, "job-1", "reel-9", "Garlic pasta") == 2
    (call,) = expo.calls
    assert call["url"] == "https://exp.host/--/api/v2/push/send"
    assert {m["to"] for m in call["messages"]} == {TOKEN, TOKEN2}  # never another user's device
    msg = call["messages"][0]
    assert msg["channelId"] == "reel-processing" and msg["data"] == {"type": "reel_ready", "reelId": "reel-9", "jobId": "job-1"}
    assert "Garlic pasta" in msg["body"]
    assert "Authorization" not in call["headers"]


def test_access_token_is_sent_only_when_configured(tokens, monkeypatch):
    db, expo = tokens
    db.rows[TOKEN] = USER
    monkeypatch.setattr(settings, "expo_access_token", "tok")
    push.notify_reel_ready(USER, "j", "r", "")
    assert expo.calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert expo.calls[0]["messages"][0]["body"] == "Your reel is ready in your vault."


def test_no_registered_device_means_no_request_at_all(tokens):
    _, expo = tokens
    assert push.notify_reel_ready(USER, "j", "r", "t") == 0 and expo.calls == []


def test_dead_device_token_is_forgotten(tokens):
    db, expo = tokens
    db.rows.update({TOKEN: USER, TOKEN2: USER})
    expo.tickets = [{"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}, {"status": "ok"}]
    assert push.notify_reel_ready(USER, "j", "r", "t") == 1
    assert TOKEN not in db.rows and TOKEN2 in db.rows


def test_push_service_trouble_never_raises(tokens, monkeypatch):
    db, expo = tokens
    db.rows[TOKEN] = USER
    expo.status_code = 503
    assert push.notify_reel_ready(USER, "j", "r", "t") == 0
    monkeypatch.setattr(push.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")))
    assert push.notify_reel_ready(USER, "j", "r", "t") == 0


# ── Token endpoints ──────────────────────────────────────────────────────────

def test_register_and_remove_own_token(env, tokens):  # noqa: F811
    _, _, client = env
    db, _ = tokens
    assert client.put("/api/user/push-token", json={"token": TOKEN}).status_code == 200
    assert db.rows == {TOKEN: USER}
    assert client.request("DELETE", "/api/user/push-token", json={"token": TOKEN}).status_code == 200
    assert db.rows == {}


def test_malformed_token_is_rejected(env, tokens):  # noqa: F811
    _, _, client = env
    for bad in ["", "abc", "ExponentPushToken[", "http://evil.example/x", "ExponentPushToken[" + "a" * 300 + "]"]:
        assert client.put("/api/user/push-token", json={"token": bad}).status_code == 400


def test_cannot_remove_another_users_token(env, tokens):  # noqa: F811
    _, _, client = env
    db, _ = tokens
    db.rows[TOKEN] = OTHER
    client.request("DELETE", "/api/user/push-token", json={"token": TOKEN})
    assert db.rows == {TOKEN: OTHER}
