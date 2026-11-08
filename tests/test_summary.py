"""summary.get_summary with a stubbed LLM backend — no real calls, no API key.

Two layers: (1) the real ``openai`` SDK driven through an httpx.MockTransport,
so SDK request building / response parsing is exercised against a recorded
chat-completions payload shape; (2) a pure fake client for failure modes.
"""

from __future__ import annotations

import json
import os

import httpx
import openai
import pytest

import summary


def completion(content):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "stub-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture
def llm(monkeypatch):
    """Route the real OpenAI SDK through MockTransport; yields the request log."""
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    seen: list[httpx.Request] = []
    state = {"handler": lambda req: httpx.Response(200, json=completion("  A plain summary.  "))}

    def handler(request):
        seen.append(request)
        return state["handler"](request)

    real_openai = openai.OpenAI

    def factory(**kwargs):
        kwargs["http_client"] = kwargs.get("http_client") or httpx.Client(
            transport=httpx.MockTransport(handler)
        )
        kwargs["max_retries"] = 0
        return real_openai(**kwargs)

    monkeypatch.setattr(openai, "OpenAI", factory)
    seen_state = type("L", (), {"requests": seen, "state": state})
    return seen_state


def test_no_llm_configured_returns_empty_without_network(monkeypatch):
    assert "OPENAI_API_KEY" not in os.environ
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: pytest.fail("client must not be built"))
    assert summary.get_summary("some text") == ""


def test_blank_text_returns_empty(llm):
    assert summary.get_summary("") == ""
    assert summary.get_summary("   \n ") == ""
    assert llm.requests == []


def test_happy_path_returns_stripped_summary_and_sends_expected_payload(llm, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "my-model")
    monkeypatch.setenv("LLM_API_KEY", "k-test")
    out = summary.get_summary("x" * 5000)
    assert out == "A plain summary."
    (req,) = llm.requests
    assert req.url.path == "/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k-test"
    body = json.loads(req.content)
    assert body["model"] == "my-model"
    assert body["messages"][0] == {"role": "system", "content": summary.SYSTEM_PROMPT}
    user = body["messages"][1]
    assert user["role"] == "user"
    assert user["content"].count("x") == 4000  # input truncated to 4000 chars


def test_default_model_and_placeholder_key(llm):
    summary.get_summary("text")
    body = json.loads(llm.requests[0].content)
    assert body["model"] == "gpt-4.1-mini"
    assert llm.requests[0].headers["authorization"] == "Bearer not-needed"


def test_null_content_gives_empty_string(llm):
    llm.state["handler"] = lambda req: httpx.Response(200, json=completion(None))
    assert summary.get_summary("text") == ""


def test_api_error_never_raises_and_logs_to_stderr(llm, capsys):
    llm.state["handler"] = lambda req: httpx.Response(
        500, json={"error": {"message": "server exploded", "type": "server_error"}}
    )
    assert summary.get_summary("text") == ""
    assert "Summary generation failed" in capsys.readouterr().err


def test_malformed_response_never_raises(llm):
    llm.state["handler"] = lambda req: httpx.Response(200, json={"choices": []})
    assert summary.get_summary("text") == ""


def test_proxy_client_is_built_closed_and_failure_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("TAILSCALE_PROXY", "http://proxy.test:3128")
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)
            self.chat = type("C", (), {"completions": self})()

        def create(self, **kw):
            return type(
                "R",
                (),
                {
                    "choices": [
                        type("Ch", (), {"message": type("M", (), {"content": "via proxy"})()})()
                    ]
                },
            )()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    assert summary.get_summary("text") == "via proxy"
    assert isinstance(captured["http_client"], httpx.Client)
    assert captured["http_client"].is_closed  # closed in the finally block

    # A proxy URL httpx cannot build must degrade to a direct connection.
    monkeypatch.setenv("TAILSCALE_PROXY", "not a url ::")
    captured.clear()
    assert summary.get_summary("text") == "via proxy"
    assert captured["http_client"] is None
