"""Tests for llm_client.py: retry backoff, circuit breaker, and error classification."""

import json
import os
import sys
import urllib.error
from email.message import Message

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
import llm_client
from llm_client import DEFAULT_RETRY_BACKOFF_CAP_SECONDS, FatalError, LlmClient, TruncatedResponseError


def _make_client(**overrides):
    client = LlmClient.__new__(LlmClient)
    client.api_key = "fake-key"
    client.model = "fake-model"
    client.base_url = "https://example.invalid"
    client.max_tokens = 32768
    client.retry_limit = 8
    client.timeout = 300
    client.lang = "English"
    client.circuit_open = False
    client.api_calls = 0
    client.ssl_context = None
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestRetryBackoff:
    def test_retry_wait_is_capped_at_sixty_seconds(self, monkeypatch):
        client = _make_client()
        waits = []

        def fail(*_args, **_kwargs):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fail)
        monkeypatch.setattr(llm_client.random, "random", lambda: 0.0)
        monkeypatch.setattr(llm_client.time, "sleep", waits.append)

        result = client.call("prompt")

        assert result.startswith("Error: Network error")
        assert waits == [2, 4, 8, 16, 32, 60.0, 60.0, 60.0]
        assert max(waits) == DEFAULT_RETRY_BACKOFF_CAP_SECONDS

    def test_server_error_attempts_bounded_by_retry_limit(self, monkeypatch):
        client = _make_client(retry_limit=3)
        attempts = {"count": 0}

        def fail(*_args, **_kwargs):
            attempts["count"] += 1
            raise urllib.error.HTTPError("https://example.invalid", 503, "unavailable", Message(), None)

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fail)
        monkeypatch.setattr(llm_client.time, "sleep", lambda _s: None)

        result = client.call("prompt")

        assert result == "Error: HTTP 503 - unavailable"
        assert attempts["count"] == 4

    def test_truncated_response_retries_then_raises(self, monkeypatch):
        client = _make_client(retry_limit=2)
        body = json.dumps(
            {"choices": [{"message": {"content": '{"short": "incomplete'}}]}
        ).encode("utf-8")
        attempts = {"count": 0}

        def fake_urlopen(*_args, **_kwargs):
            attempts["count"] += 1
            return _FakeResponse(body)

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(TruncatedResponseError):
            client.call("prompt")
        assert attempts["count"] == 3

    def test_truncated_then_complete_response_recovers(self, monkeypatch):
        client = _make_client(retry_limit=2)
        truncated = json.dumps(
            {"choices": [{"message": {"content": '{"short": "incomplete'}}]}
        ).encode("utf-8")
        complete = json.dumps(
            {"choices": [{"message": {"content": '{"short": "done"}'}}]}
        ).encode("utf-8")
        bodies = [truncated, complete]

        def fake_urlopen(*_args, **_kwargs):
            return _FakeResponse(bodies.pop(0))

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)

        assert client.call("prompt") == '{"short": "done"}'
        assert bodies == []


class TestResponseParsing:
    def test_markdown_fence_is_stripped(self, monkeypatch):
        client = _make_client()
        content = "```json\n{\"short\": \"fenced\"}\n```"
        body = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode("utf-8")

        monkeypatch.setattr(
            llm_client.urllib.request, "urlopen",
            lambda *_a, **_k: _FakeResponse(body),
        )

        assert client.call("prompt") == '{"short": "fenced"}'


class TestCircuitBreaker:
    def test_fatal_http_code_opens_circuit_and_raises(self, monkeypatch):
        client = _make_client()
        attempts = {"count": 0}

        def fail(*_args, **_kwargs):
            attempts["count"] += 1
            raise urllib.error.HTTPError("https://example.invalid", 401, "unauthorized", Message(), None)

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fail)

        with pytest.raises(FatalError):
            client.call("prompt")
        assert client.circuit_open is True

        result = client.call("prompt")
        assert result == "Error: Circuit breaker open."
        assert attempts["count"] == 1

    def test_missing_api_key_short_circuits_without_network(self, monkeypatch):
        client = _make_client(api_key=None)

        def fail(*_args, **_kwargs):
            raise AssertionError("network must not be touched")

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fail)

        assert client.call("prompt") == "Error: REMY_LLM_API_KEY not set."
        assert client.api_calls == 0
