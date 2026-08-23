"""Tests for llm_client.py: retry backoff, circuit breaker, and error classification."""

import json
import os
import ssl
import sys
import urllib.error
from email.message import Message
from pathlib import Path

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


class _FakeConfig:
    """ConfigSnapshot stand-in for the real-constructor path."""

    _DEFAULTS = {
        "REMY_LLM_API_KEY": "fake-key",
        "REMY_LLM_MODEL": "fake-model",
        "REMY_LLM_BASE_URL": "https://example.invalid",
        "REMY_LLM_MAX_TOKENS": 32768,
        "REMY_LLM_RETRY_LIMIT": 8,
        "REMY_LLM_TIMEOUT": 300,
        "REMY_LLM_TLS_INSECURE": False,
    }

    def __init__(self, **overrides):
        self._values = {**self._DEFAULTS, **overrides}

    def get(self, key, default=None):
        return self._values.get(key, default)

    def get_int(self, key):
        return self._values[key]

    def get_bool(self, key):
        return self._values[key]


class TestTlsConfiguration:
    def test_default_context_verifies_certificates(self):
        client = LlmClient(config=_FakeConfig())

        assert client.ssl_context.check_hostname is True
        assert client.ssl_context.verify_mode == ssl.CERT_REQUIRED

    def test_insecure_flag_disables_verification(self):
        client = LlmClient(config=_FakeConfig(REMY_LLM_TLS_INSECURE=True))

        assert client.ssl_context.check_hostname is False
        assert client.ssl_context.verify_mode == ssl.CERT_NONE

    @pytest.mark.parametrize(
        "raw_value, expected_verify, expected_hostname",
        [("false", ssl.CERT_REQUIRED, True), ("true", ssl.CERT_NONE, False)],
    )
    def test_default_constructor_reads_key_through_load_config(
        self, tmp_path, monkeypatch, raw_value, expected_verify, expected_hostname
    ):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        for key in llm_client.remy_config.FIELD_SPECS:
            monkeypatch.delenv(key, raising=False)
        (home / ".claude" / "remy-config.json").write_text(
            json.dumps({
                "schema_version": "1.0.0",
                "values": {"REMY_LLM_TLS_INSECURE": raw_value},
            }),
            encoding="utf-8",
        )

        client = LlmClient()

        assert client.ssl_context.verify_mode == expected_verify
        assert client.ssl_context.check_hostname is expected_hostname

    def test_cert_verification_error_fails_fast_without_retry(self, monkeypatch):
        client = _make_client()
        attempts = {"count": 0}

        def fail(*_args, **_kwargs):
            attempts["count"] += 1
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")
            )

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fail)
        monkeypatch.setattr(
            llm_client.time, "sleep",
            lambda _s: pytest.fail("cert failure must not enter the retry loop"),
        )

        result = client.call("prompt")

        assert result.startswith("Error: TLS certificate verification failed")
        assert "REMY_LLM_TLS_INSECURE=true" in result
        assert attempts["count"] == 1
