"""Per-reel cost logging, the OpenRouter provider path, and tolerance for sloppy model output.
No network: httpx.post and the Gemini SDK client are replaced with fakes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pipeline import gemini_client as g
from pipeline import orchestrator
from pipeline.config import settings
from pipeline.models import ActionData


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    g._usage.set(None)
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-test")
    yield
    g._usage.set(None)


def _openrouter_reply(content: str, *, prompt=100, completion=20, cost=0.001, status=200):
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "cost": cost},
    }
    return httpx.Response(status, json=body, request=httpx.Request("POST", g.OPENROUTER_URL))


# ── usage tracking ──────────────────────────────────────────────────────────


def test_record_usage_is_noop_when_not_tracking():
    g._record_usage("vision", "gemini-2.5-flash-lite", 10, 5, 0.1)
    assert g.pop_usage() == []


def test_pop_usage_returns_calls_and_stops_tracking():
    g.start_usage_tracking()
    g._record_usage("vision", "gemini-2.5-flash-lite", 10, 5, 0.25)
    calls = g.pop_usage()
    assert calls == [
        {"step": "vision", "model": "gemini-2.5-flash-lite", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25}
    ]
    g._record_usage("vision", "gemini-2.5-flash-lite", 1, 1, 1.0)  # tracking is off again
    assert g.pop_usage() == []


def test_missing_cost_is_priced_from_token_table():
    g.start_usage_tracking()
    g._record_usage("summary", "gemini-2.5-flash-lite", 1_000_000, 1_000_000, None)
    (call,) = g.pop_usage()
    assert call["cost_usd"] == pytest.approx(0.10 + 0.40)  # $0.10 in + $0.40 out per 1M


def test_tracking_is_isolated_between_reels():
    g.start_usage_tracking()
    g._record_usage("vision", "m", 1, 1, 0.5)
    g.start_usage_tracking()  # next reel starts clean
    assert g.pop_usage() == []


# ── OpenRouter path ─────────────────────────────────────────────────────────


def test_openrouter_request_shape_and_cost_recorded(monkeypatch):
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, body=json)
        return _openrouter_reply('{"ok": true}', prompt=1234, completion=56, cost=0.00042)

    monkeypatch.setattr(httpx, "post", fake_post)
    g.start_usage_tracking()
    text = g._generate("gemini-2.5-flash-lite", ["hello"], 0.3, "summary")

    assert text == '{"ok": true}'
    assert sent["url"] == g.OPENROUTER_URL
    assert sent["headers"]["Authorization"] == "Bearer sk-or-test"
    assert sent["body"]["model"] == "google/gemini-2.5-flash-lite"  # bare id gets the provider prefix
    assert sent["body"]["response_format"] == {"type": "json_object"}
    assert sent["body"]["usage"] == {"include": True}
    assert sent["body"]["messages"][0]["content"] == [{"type": "text", "text": "hello"}]
    (call,) = g.pop_usage()
    assert (call["step"], call["input_tokens"], call["output_tokens"], call["cost_usd"]) == ("summary", 1234, 56, 0.00042)


def test_openrouter_model_with_slash_is_not_prefixed(monkeypatch):
    seen = {}
    monkeypatch.setattr(httpx, "post", lambda url, headers, json, timeout: seen.update(m=json["model"]) or _openrouter_reply("{}"))
    g._generate("google/gemini-2.5-flash-lite", ["x"], 0.0)
    assert seen["m"] == "google/gemini-2.5-flash-lite"


def test_openrouter_error_status_raises_with_body(monkeypatch):
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: httpx.Response(402, text="needs $0.50 balance", request=httpx.Request("POST", g.OPENROUTER_URL)),
    )
    with pytest.raises(RuntimeError, match="OpenRouter error 402.*needs \\$0.50"):
        g._generate("m", ["x"], 0.0)


def test_openrouter_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    with pytest.raises(g.GeminiNotConfigured, match="OPENROUTER_API_KEY"):
        g._generate("m", ["x"], 0.0)


def test_openrouter_content_conversion():
    content = g._openrouter_content(
        [
            "prompt",
            {"inline_data": {"mime_type": "image/jpeg", "data": b"\xff\xd8"}},
            {"inline_data": {"mime_type": "audio/mp4", "data": b"abc"}},
            {"inline_data": {"mime_type": "audio/mpeg", "data": b"abc"}},
        ]
    )
    assert content[0] == {"type": "text", "text": "prompt"}
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[2] == {"type": "input_audio", "input_audio": {"data": "YWJj", "format": "m4a"}}
    assert content[3]["input_audio"]["format"] == "mp3"


def test_gemini_provider_path_records_sdk_usage(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    fake = SimpleNamespace(
        text="[]",
        usage_metadata=SimpleNamespace(prompt_token_count=500, candidates_token_count=40),
    )
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: fake))
    monkeypatch.setattr(g, "get_client", lambda: client)
    g.start_usage_tracking()
    assert g._generate("gemini-3.1-flash-lite", ["x"], 0.1, "vision") == "[]"
    (call,) = g.pop_usage()
    assert (call["input_tokens"], call["output_tokens"]) == (500, 40)
    assert call["cost_usd"] == pytest.approx((500 * 0.25 + 40 * 1.50) / 1e6)


def test_gemini_provider_without_usage_metadata_does_not_crash(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    fake = SimpleNamespace(text="[]", usage_metadata=None)
    monkeypatch.setattr(g, "get_client", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: fake)))
    g.start_usage_tracking()
    g._generate("m", ["x"], 0.1)
    assert g.pop_usage() == []


# ── malformed model output ──────────────────────────────────────────────────


def test_generate_json_retries_malformed_json_and_counts_each_attempt(monkeypatch):
    replies = iter(['{"a": ', "{bad json}", '{"ok": 1}'])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _openrouter_reply(next(replies), cost=0.001))
    g.start_usage_tracking()
    assert g._generate_json("m", ["x"], 0.1, "vision") == {"ok": 1}
    assert len(g.pop_usage()) == 3  # failed attempts are still billed, so still logged


def test_generate_json_gives_up_after_three_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1) or _openrouter_reply("not json"))
    with pytest.raises(json.JSONDecodeError):
        g._generate_json("m", ["x"], 0.1, "vision")
    assert len(calls) == 3


def test_generate_json_does_not_retry_http_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: calls.append(1) or httpx.Response(500, text="boom", request=httpx.Request("POST", g.OPENROUTER_URL)),
    )
    with pytest.raises(RuntimeError, match="500"):
        g._generate_json("m", ["x"], 0.1, "vision")
    assert len(calls) == 1


def test_extract_json_strips_code_fences():
    assert g._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"frames": [1, 2]}, [1, 2]),  # object-wrapped array is unwrapped
        ([1, 2], [1, 2]),
        ({"a": 1, "b": [1]}, {"a": 1, "b": [1]}),  # multi-key objects left alone
        ({"a": "x"}, {"a": "x"}),
    ],
)
def test_as_list(raw, expected):
    assert g._as_list(raw) == expected


def test_as_text_and_as_str_list():
    assert g._as_text(["1. A", "- b"]) == "1. A\n- b"
    assert g._as_text(None) == ""
    assert g._as_text(5) == "5"
    assert g._as_str_list(None) == []
    assert g._as_str_list("one") == ["one"]
    assert g._as_str_list(["a", {"x": 1}, ["p", "q"]]) == ["a", "{'x': 1}", "p\nq"]


def test_generate_summary_survives_list_valued_fields(monkeypatch):
    sloppy = {
        "summary": "s", "category": "Tech", "action_type": "code", "tags": ["a"],
        "action_data": {"code": ["x = 1", "y = 2"], "steps": "just one", "ingredients": None,
                        "extracted_content": ["1. Title", "- bullet"]},
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _openrouter_reply(json.dumps(sloppy)))
    out = g.generate_summary("transcript", "visual")
    action = ActionData(**out["action_data"])  # the exact validation that used to 500
    assert action.code == "x = 1\ny = 2"
    assert action.steps == ["just one"]
    assert action.ingredients == []
    assert action.extracted_content == "1. Title\n- bullet"


def test_generate_summary_empty_action_data(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _openrouter_reply('{"summary": "s"}'))
    out = g.generate_summary("", "")
    ActionData(**out["action_data"])
    assert out["action_data"]["code"] is None and out["category"] == "Other"


def test_transcribe_audio_filters_empty_and_coerces(monkeypatch, tmp_path: Path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    reply = json.dumps([{"time": "3", "text": " hi "}, {"time": 4, "text": "  "}, {"text": "no time"}, "junk"])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _openrouter_reply(reply))
    assert g.transcribe_audio(audio) == [{"time": 3.0, "text": "hi"}, {"time": 0.0, "text": "no time"}]


def test_embed_text_records_estimated_usage(monkeypatch):
    resp = SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1, 0.2])])
    monkeypatch.setattr(g, "get_client", lambda: SimpleNamespace(models=SimpleNamespace(embed_content=lambda **kw: resp)))
    g.start_usage_tracking()
    assert g.embed_text("x" * 400) == [0.1, 0.2]
    (call,) = g.pop_usage()
    assert (call["step"], call["input_tokens"]) == ("embedding", 100)
    assert call["cost_usd"] == pytest.approx(100 * 0.15 / 1e6)


# ── log line ────────────────────────────────────────────────────────────────


def test_log_reel_cost_line(caplog):
    calls = [
        {"step": "vision", "model": "m", "input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.001},
        {"step": "vision", "model": "m", "input_tokens": 500, "output_tokens": 50, "cost_usd": 0.0005},
        {"step": "audio", "model": "m", "input_tokens": 200, "output_tokens": 20, "cost_usd": 0.0002},
    ]
    with caplog.at_level(logging.INFO, logger="reelvault.cost"):
        orchestrator._log_reel_cost("https://example.com/r", calls)
    (line,) = [r.getMessage() for r in caplog.records]
    assert "Reel cost ₹0.145 ($0.00170)" in line  # 0.0017 * 85
    assert "vision ₹0.128" in line and "audio ₹0.017" in line  # vision calls are summed per step
    assert "3 API calls, 1700 in / 170 out tokens" in line
    assert line.endswith("https://example.com/r")


def test_log_reel_cost_silent_when_no_calls(caplog):
    with caplog.at_level(logging.INFO, logger="reelvault.cost"):
        orchestrator._log_reel_cost("u", [])
    assert caplog.records == []


# ── full pipeline wiring ────────────────────────────────────────────────────


def _stub_pipeline(monkeypatch, tmp_path):
    from pipeline.extractors.video import create_mock_metadata

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "dry_run", False)
    metadata = create_mock_metadata("https://example.com/r", 10.0, 30.0)
    monkeypatch.setattr(orchestrator, "fetch_reel", lambda url, d, download_video=True: metadata)
    monkeypatch.setattr(orchestrator, "extract_frames", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "analyze_frames", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "transcribe_audio", lambda *a, **k: [])

    def fake_summary(*a, **k):
        g._record_usage("summary", "gemini-2.5-flash-lite", 100, 10, 0.0001)
        return {"summary": "s", "category": "Other", "action_type": "general", "action_data": {}, "tags": []}

    monkeypatch.setattr(orchestrator, "generate_summary", fake_summary)
    monkeypatch.setattr(orchestrator, "generate_embedding", lambda *a, **k: [0.0])


def test_process_reel_logs_cost_on_success(monkeypatch, tmp_path, caplog):
    _stub_pipeline(monkeypatch, tmp_path)
    with caplog.at_level(logging.INFO, logger="reelvault.cost"):
        orchestrator.process_reel("https://example.com/r", dry_run=False, user_id="u", persist_json=False)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "Reel cost ₹0.009" in lines[0] and "summary" in lines[0]
    assert g._usage.get() is None  # tracking stopped


def test_process_reel_failure_logs_nothing_and_stops_tracking(monkeypatch, tmp_path, caplog):
    _stub_pipeline(monkeypatch, tmp_path)

    def boom(*a, **k):
        g._record_usage("summary", "m", 1, 1, 0.5)
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(orchestrator, "generate_summary", boom)
    with caplog.at_level(logging.INFO, logger="reelvault.cost"):
        with pytest.raises(RuntimeError, match="exploded"):
            orchestrator.process_reel("https://example.com/r", dry_run=False, user_id="u", persist_json=False)
    assert caplog.records == []
    assert g._usage.get() is None


def test_save_route_logs_failure_traceback(monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from api import deps
    from api.main import app
    from api.rate_limit import _buckets
    from api.routes import reels

    _buckets.clear()
    app.dependency_overrides[deps.require_active_subscription] = lambda: deps.CurrentUser(id="u", email="a@b.c")
    monkeypatch.setattr(reels, "process_reel", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model blew up")))
    try:
        with caplog.at_level(logging.ERROR, logger="reelvault"):
            r = TestClient(app).post("/api/reels/save", json={"url": "https://www.instagram.com/p/abc/"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 500 and "model blew up" in r.json()["error"]
    assert any("Reel processing failed" in rec.getMessage() and rec.exc_info for rec in caplog.records)
