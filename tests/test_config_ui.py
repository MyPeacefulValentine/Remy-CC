import contextlib
import importlib.util
import json
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import urllib.error
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REMY_SRC = Path(__file__).resolve().parent.parent / "remy-src"
spec_config = importlib.util.spec_from_file_location("remy_config", REMY_SRC / "remy_config.py")
assert spec_config and spec_config.loader
remy_config = importlib.util.module_from_spec(spec_config)
sys.modules["remy_config"] = remy_config
spec_config.loader.exec_module(remy_config)

spec = importlib.util.spec_from_file_location("config_ui", REMY_SRC / "config_ui.py")
assert spec and spec.loader
config_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config_ui)

FAKE_SECRET = "test-secret-unique-marker"


def _write(path, values, schema="1.0.0"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": schema, "values": values}),
        encoding="utf-8",
    )


@pytest.fixture
def ui_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home if cls else home))
    monkeypatch.setattr(config_ui.Path, "home", classmethod(lambda cls: home if cls else home))
    for key in remy_config.FIELD_SPECS:
        monkeypatch.delenv(key, raising=False)
    config_ui.ConfigHandler.mode = "global"
    config_ui.ConfigHandler.target_path = None
    config_ui.ConfigHandler.project_root = None
    config_ui.ConfigHandler.html_path = REMY_SRC / "config_ui.html"
    config_ui.ConfigHandler.session_token = "t" * 43
    config_ui.ConfigHandler.test_lock = threading.Lock()
    with config_ui.ConfigHandler.activity_lock:
        config_ui.ConfigHandler.active_requests = 0
    return home


@contextlib.contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), config_ui.ConfigHandler)
    port = server.server_address[1]
    config_ui.ConfigHandler.expected_authority = f"127.0.0.1:{port}"
    config_ui.ConfigHandler.expected_origin = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _request(port, method, path, payload=None, headers=None, add_post_auth=True):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if method == "POST":
        request_headers.setdefault("Content-Type", "application/json")
        if add_post_auth:
            request_headers.setdefault("Origin", f"http://127.0.0.1:{port}")
            request_headers.setdefault("X-Remy-Session", config_ui.ConfigHandler.session_token)
    if body is not None:
        request_headers.setdefault("Content-Length", str(len(body)))
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    data = json.loads(raw.decode("utf-8")) if raw else {}
    result_headers = dict(response.getheaders())
    connection.close()
    return response.status, data, result_headers


def _raw_request(port, request_bytes, shutdown_write=True):
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    connection.sendall(request_bytes)
    if shutdown_write:
        connection.shutdown(socket.SHUT_WR)
    response = bytearray()
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        response.extend(chunk)
    connection.close()
    head, _, body = bytes(response).partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, head, body


@contextlib.contextmanager
def _upstream(status=200, payload=None, headers=None):
    state = {"requests": []}
    response_payload = payload if payload is not None else {
        "choices": [{"message": {"content": "ok"}}]
    }
    response_body = response_payload if isinstance(response_payload, bytes) else json.dumps(response_payload).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            _ = format, args

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            state["requests"].append({"headers": dict(self.headers), "body": body})
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _test_payload(base_url, action="replace", api_key=FAKE_SECRET, model="fake-model"):
    return {
        "api_key_action": action,
        "api_key": api_key if action == "replace" else "",
        "base_url": base_url,
        "model": model,
    }


def test_get_redacts_secret_and_reports_source(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"REMY_LLM_API_KEY": FAKE_SECRET, "REMY_LANG": "zh-CN"})
    with _server() as port:
        status, payload, headers = _request(port, "GET", "/api/config")
    encoded = json.dumps(payload)
    assert status == 200
    assert FAKE_SECRET not in encoded
    assert payload["secret_state"]["REMY_LLM_API_KEY"] == {"has_value": True, "source": "user"}
    assert payload["sources"]["REMY_LANG"] == "user"
    assert payload["read_only"] is False
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_sparse_save_preserves_unknown_and_secret(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"FUTURE_FIELD": "keep", "REMY_LLM_API_KEY": FAKE_SECRET})
    with _server() as port:
        status, payload, _ = _request(port, "POST", "/api/save", {"values": {"REMY_LANG": "zh-CN"}, "clear_secrets": []})
    values = remy_config.read_document(path)["values"]
    assert status == 200 and payload["status"] == "ok"
    assert values == {"FUTURE_FIELD": "keep", "REMY_LLM_API_KEY": FAKE_SECRET, "REMY_LANG": "zh-CN"}


def test_replace_and_clear_secret(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"REMY_LLM_API_KEY": "first"})
    with _server() as port:
        assert _request(port, "POST", "/api/save", {"values": {"REMY_LLM_API_KEY": "second"}})[0] == 200
        assert remy_config.read_document(path)["values"]["REMY_LLM_API_KEY"] == "second"
        assert _request(port, "POST", "/api/save", {"values": {}, "clear_secrets": ["REMY_LLM_API_KEY"]})[0] == 200
    assert "REMY_LLM_API_KEY" not in remy_config.read_document(path)["values"]


def test_project_rejects_secret_and_removes_disabled_override(ui_home, tmp_path):
    _ = ui_home
    project = tmp_path / "project"
    target = project / ".claude" / "remy-config.json"
    _write(target, {"REMY_LANG": "zh-CN", "REMY_LLM_MAX_WORKERS": "4"})
    config_ui.ConfigHandler.mode = "project"
    config_ui.ConfigHandler.project_root = project
    config_ui.ConfigHandler.target_path = target
    with _server() as port:
        status, _, _ = _request(port, "POST", "/api/save", {
            "values": {"REMY_LANG": "en", "REMY_LLM_API_KEY": FAKE_SECRET},
            "overrides": ["REMY_LANG", "REMY_LLM_API_KEY"],
        })
        assert status == 400
        status, payload, _ = _request(port, "POST", "/api/save", {
            "values": {"REMY_LANG": "en"}, "overrides": ["REMY_LANG"]
        })
        test_status, _, _ = _request(port, "POST", "/api/test-llm", _test_payload("http://127.0.0.1:1"))
    assert status == 200 and payload["status"] == "ok"
    assert test_status == 400
    assert remy_config.read_document(target, project=True)["values"] == {"REMY_LANG": "en"}


def test_remove_keys_deletes_explicit_values_and_preserves_rest(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {
        "REMY_LANG": "zh-CN",
        "REMY_LLM_MAX_WORKERS": "4",
        "FUTURE_FIELD": "keep",
        "REMY_LLM_API_KEY": FAKE_SECRET,
    })
    with _server() as port:
        status, payload, _ = _request(port, "POST", "/api/save", {
            "values": {},
            "clear_secrets": [],
            "remove_keys": ["REMY_LANG", "REMY_LLM_MAX_WORKERS"],
        })
        combined_status, _, _ = _request(port, "POST", "/api/save", {
            "values": {"REMY_BANNER_ENABLED": "false"},
            "remove_keys": ["REMY_LLM_MAX_WORKERS"],
        })
    assert status == 200 and payload["status"] == "ok"
    assert combined_status == 200
    values = remy_config.read_document(path)["values"]
    assert values == {
        "FUTURE_FIELD": "keep",
        "REMY_LLM_API_KEY": FAKE_SECRET,
        "REMY_BANNER_ENABLED": "false",
    }


def test_remove_keys_rejections_leave_file_unchanged(ui_home, tmp_path):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"REMY_LANG": "zh-CN"})
    before = path.read_bytes()
    invalid_payloads = [
        {"values": {}, "remove_keys": "REMY_LANG"},
        {"values": {}, "remove_keys": [1]},
        {"values": {}, "remove_keys": ["REMY_LANG", "REMY_LANG"]},
        {"values": {}, "remove_keys": ["UNKNOWN_FIELD"]},
        {"values": {}, "remove_keys": ["REMY_LLM_API_KEY"]},
        {"values": {}, "remove_keys": ["REMY_LANG"], "reset_mode": "all"},
        {"values": {}, "remove_keys": ["REMY_LANG"], "reset_mode": "non_secret"},
    ]
    with _server() as port:
        for payload in invalid_payloads:
            status, body, _ = _request(port, "POST", "/api/save", payload)
            assert status == 400 and body["status"] == "error"
            assert path.read_bytes() == before
    project = tmp_path / "project"
    target = project / ".claude" / "remy-config.json"
    _write(target, {"REMY_LANG": "zh-CN"})
    project_before = target.read_bytes()
    config_ui.ConfigHandler.mode = "project"
    config_ui.ConfigHandler.project_root = project
    config_ui.ConfigHandler.target_path = target
    with _server() as port:
        status, _, _ = _request(port, "POST", "/api/save", {
            "values": {},
            "overrides": [],
            "remove_keys": ["REMY_LANG"],
        })
    assert status == 400
    assert target.read_bytes() == project_before


def test_get_registry_exposes_ui_metadata(ui_home):
    _ = ui_home
    with _server() as port:
        status, payload, _ = _request(port, "GET", "/api/config")
    assert status == 200
    registry = payload["registry"]
    assert len(registry) == 57
    assert [group["id"] for group in payload["groups"]] == [
        "llm_api", "index_generation", "injection", "mcp", "summary", "timeline", "system",
    ]
    for row in registry:
        assert row["label_en"] and row["label_zh"]
        assert row["restart_scope"] in ("immediate", "next_index", "next_session", "next_mcp_launch")
        assert isinstance(row["advanced"], bool)
        assert ("unit_en" in row) == ("unit_zh" in row)


def test_reset_modes_preserve_unknown_and_enforce_secret_boundary(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"FUTURE_FIELD": "keep", "REMY_LLM_API_KEY": FAKE_SECRET, "REMY_LANG": "zh-CN"})
    before = path.read_bytes()
    with _server() as port:
        old_status, _, _ = _request(port, "POST", "/api/save", {"values": {}, "reset": True})
        assert path.read_bytes() == before
        invalid_status, _, _ = _request(port, "POST", "/api/save", {"values": {}, "reset_mode": "invalid"})
        assert path.read_bytes() == before
        typed_status, _, _ = _request(port, "POST", "/api/save", {"values": {}, "reset_mode": 1})
        assert path.read_bytes() == before
        mixed_status, _, _ = _request(port, "POST", "/api/save", {"values": {"REMY_LANG": "en"}, "reset_mode": "non_secret"})
        assert path.read_bytes() == before
        non_secret_status, _, _ = _request(port, "POST", "/api/save", {"values": {}, "clear_secrets": [], "reset_mode": "non_secret"})
        after_non_secret = remy_config.read_document(path)["values"]
        all_status, _, _ = _request(port, "POST", "/api/save", {"values": {}, "clear_secrets": [], "reset_mode": "all"})
    assert (old_status, invalid_status, typed_status, mixed_status) == (400, 400, 400, 400)
    assert non_secret_status == 200
    assert after_non_secret == {"FUTURE_FIELD": "keep", "REMY_LLM_API_KEY": FAKE_SECRET}
    assert all_status == 200
    assert remy_config.read_document(path)["values"] == {"FUTURE_FIELD": "keep"}


def test_project_all_reset_removes_overrides_and_preserves_unknown(ui_home, tmp_path):
    _ = ui_home
    project = tmp_path / "project"
    target = project / ".claude" / "remy-config.json"
    _write(target, {"FUTURE_FIELD": "keep", "REMY_LANG": "zh-CN"})
    config_ui.ConfigHandler.mode = "project"
    config_ui.ConfigHandler.project_root = project
    config_ui.ConfigHandler.target_path = target
    with _server() as port:
        status, _, _ = _request(port, "POST", "/api/save", {"values": {}, "clear_secrets": [], "reset_mode": "all"})
    assert status == 200
    assert remy_config.read_document(target, project=True)["values"] == {"FUTURE_FIELD": "keep"}


def test_project_rejects_non_secret_reset(ui_home, tmp_path):
    _ = ui_home
    project = tmp_path / "project"
    target = project / ".claude" / "remy-config.json"
    _write(target, {"REMY_LANG": "zh-CN"})
    before = target.read_bytes()
    config_ui.ConfigHandler.mode = "project"
    config_ui.ConfigHandler.project_root = project
    config_ui.ConfigHandler.target_path = target
    with _server() as port:
        status, _, _ = _request(port, "POST", "/api/save", {"values": {}, "clear_secrets": [], "reset_mode": "non_secret"})
    assert status == 400
    assert target.read_bytes() == before


def test_post_source_and_media_type_guards(ui_home):
    _ = ui_home
    with _server() as port:
        good = _test_payload("http://127.0.0.1:1")
        for headers in (
            {"Host": "evil.invalid", "Origin": f"http://127.0.0.1:{port}", "X-Remy-Session": config_ui.ConfigHandler.session_token},
            {"Host": f"127.0.0.1:{port}", "Origin": "https://evil.invalid", "X-Remy-Session": config_ui.ConfigHandler.session_token},
            {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "X-Remy-Session": "wrong"},
            {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "X-Remy-Session": "é"},
            {"Host": f"127.0.0.1:{port}", "X-Remy-Session": config_ui.ConfigHandler.session_token},
            {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"},
        ):
            status, payload, response_headers = _request(
                port, "POST", "/api/test-llm", good, headers, add_post_auth=False
            )
            assert status == 403 and payload == {"status": "error", "message": "forbidden"}
            assert response_headers["Cache-Control"] == "no-store"
            assert response_headers["Referrer-Policy"] == "no-referrer"
            assert response_headers["X-Content-Type-Options"] == "nosniff"
        status, payload, response_headers = _request(port, "POST", "/api/test-llm", good, {"Content-Type": "text/plain"})
        assert status == 415 and payload["message"] == "unsupported_media_type"
        assert response_headers["Cache-Control"] == "no-store"
        unknown_status, _, _ = _request(port, "POST", "/api/unknown", {})
        shutdown_status, _, _ = _request(port, "POST", "/api/shutdown", {"unexpected": True})
        assert unknown_status == 404
        assert shutdown_status == 400


def test_content_length_guards_and_boundaries(ui_home):
    _ = ui_home
    with _server() as port:
        authority = f"127.0.0.1:{port}"
        common = (
            f"Host: {authority}\r\n"
            f"Origin: http://{authority}\r\n"
            f"X-Remy-Session: {config_ui.ConfigHandler.session_token}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n"
        )
        for length_value in (None, "not-a-number", "-1", str(config_ui.MAX_REQUEST_BYTES + 1)):
            length_header = "" if length_value is None else f"Content-Length: {length_value}\r\n"
            request = (f"POST /api/test-llm HTTP/1.1\r\n{common}{length_header}\r\n").encode("ascii")
            status, _, body = _raw_request(port, request)
            assert status == 400
            assert b"invalid_content_length" in body

        incomplete = (
            f"POST /api/test-llm HTTP/1.1\r\n{common}Content-Length: 10\r\n\r\n{{}}"
        ).encode("ascii")
        status, _, body = _raw_request(port, incomplete)
        assert status == 400
        assert b"incomplete_request" in body

        for padding_length in (
            config_ui.MAX_REQUEST_BYTES - len('{"padding":""}') - 1,
            config_ui.MAX_REQUEST_BYTES - len('{"padding":""}'),
        ):
            body_bytes = json.dumps({"padding": "x" * padding_length}, separators=(",", ":")).encode("utf-8")
            assert len(body_bytes) in (config_ui.MAX_REQUEST_BYTES - 1, config_ui.MAX_REQUEST_BYTES)
            request = (
                f"POST /api/test-llm HTTP/1.1\r\n{common}Content-Length: {len(body_bytes)}\r\n\r\n"
            ).encode("ascii") + body_bytes
            status, _, _ = _raw_request(port, request)
            assert status == 400


def test_html_nonce_csp_and_dynamic_headers(ui_home):
    _ = ui_home
    with _server() as port:
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/")
        first = connection.getresponse()
        first_html = first.read().decode("utf-8")
        first_headers = dict(first.getheaders())
        connection.close()
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/")
        second = connection.getresponse()
        second_html = second.read().decode("utf-8")
        second_headers = dict(second.getheaders())
        connection.close()
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/logo.svg")
        logo = connection.getresponse()
        logo.read()
        logo_headers = dict(logo.getheaders())
        connection.close()
    for response_headers in (first_headers, second_headers):
        assert response_headers["Cache-Control"] == "no-store"
        assert response_headers["Referrer-Policy"] == "no-referrer"
        assert response_headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in response_headers["Content-Security-Policy"]
    assert first_headers["Content-Security-Policy"] != second_headers["Content-Security-Policy"]
    first_csp_nonce = re.search(r"script-src 'nonce-([^']+)'", first_headers["Content-Security-Policy"])
    first_html_nonce = re.search(r'<script nonce="([^"]+)">', first_html)
    second_csp_nonce = re.search(r"script-src 'nonce-([^']+)'", second_headers["Content-Security-Policy"])
    second_html_nonce = re.search(r'<script nonce="([^"]+)">', second_html)
    assert first_csp_nonce and first_html_nonce and second_csp_nonce and second_html_nonce
    assert first_csp_nonce.group(1) == first_html_nonce.group(1)
    assert second_csp_nonce.group(1) == second_html_nonce.group(1)
    assert first_csp_nonce.group(1) != second_csp_nonce.group(1)
    assert len(first_csp_nonce.group(1)) >= 22
    assert first_html.count('<script nonce="') == 1
    assert config_ui.ConfigHandler.session_token != first_csp_nonce.group(1)
    assert len(config_ui.ConfigHandler.session_token) >= 22
    assert "__REMY_SESSION_TOKEN__" not in first_html
    assert "__REMY_CSP_NONCE__" not in first_html
    assert ' onclick="' not in first_html and ' onerror="' not in first_html
    assert ("t" * 43) in first_html
    assert first_html != second_html
    assert logo_headers["X-Content-Type-Options"] == "nosniff"
    assert "Cache-Control" not in logo_headers


def test_no_heartbeat_timeout_shutdown(ui_home):
    _ = ui_home
    assert not hasattr(config_ui, "_watchdog_should_shutdown")
    assert not hasattr(config_ui, "_heartbeat_watchdog")
    assert not hasattr(config_ui, "HEARTBEAT_TIMEOUT")
    assert not hasattr(config_ui, "STARTUP_GRACE")
    config_ui.ConfigHandler._begin_request()
    assert config_ui.ConfigHandler._active_request_count() == 1
    config_ui.ConfigHandler._end_request()
    assert config_ui.ConfigHandler._active_request_count() == 0


def test_request_counter_nested_and_exception_cleanup(ui_home, monkeypatch):
    _ = ui_home
    config_ui.ConfigHandler._begin_request()
    config_ui.ConfigHandler._begin_request()
    config_ui.ConfigHandler._end_request()
    assert config_ui.ConfigHandler._active_request_count() == 1
    config_ui.ConfigHandler._end_request()
    assert config_ui.ConfigHandler._active_request_count() == 0

    def fail_save(_self, _data):
        _ = _self, _data
        raise RuntimeError("failure")

    monkeypatch.setattr(config_ui.ConfigHandler, "_save", fail_save)
    with _server() as port:
        status, payload, _ = _request(port, "POST", "/api/save", {"values": {}})
    active = config_ui.ConfigHandler._active_request_count()
    assert status == 500
    assert payload == {"status": "error", "message": "RuntimeError"}
    assert active == 0


def test_invalid_file_is_read_only_and_save_is_rejected(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    path.parent.mkdir(parents=True)
    original = b"{broken"
    path.write_bytes(original)
    with _server() as port:
        status, payload, _ = _request(port, "GET", "/api/config")
        save_status, save_payload, _ = _request(port, "POST", "/api/save", {"values": {"REMY_LANG": "zh-CN"}})
    assert status == 200
    assert payload["read_only"] is True
    assert save_status == 400 and save_payload["status"] == "error"
    assert path.read_bytes() == original


def test_llm_test_lock_rejects_concurrent_request_and_recovers(ui_home, monkeypatch):
    _ = ui_home
    release = threading.Event()
    entered = threading.Event()

    def blocking_probe(api_key, base_url, model):
        _ = api_key, base_url, model
        entered.set()
        assert release.wait(5)
        return {"status": "ok", "category": "success", "http_status": 200, "latency_ms": 1}

    monkeypatch.setattr(config_ui, "_probe_llm", blocking_probe)
    with _server() as port:
        first_result = []

        def first_request():
            first_result.append(_request(port, "POST", "/api/test-llm", _test_payload("http://127.0.0.1:1")))

        thread = threading.Thread(target=first_request)
        thread.start()
        assert entered.wait(5)
        second_status, second_payload, _ = _request(port, "POST", "/api/test-llm", _test_payload("http://127.0.0.1:1"))
        assert second_status == 409
        assert second_payload["category"] == "busy"
        active = config_ui.ConfigHandler._active_request_count()
        assert active == 1
        release.set()
        thread.join(5)
        assert first_result[0][1]["category"] == "success"
        third_status, third_payload, _ = _request(port, "POST", "/api/test-llm", _test_payload("http://127.0.0.1:1"))
        assert third_status == 200
        assert third_payload["category"] == "success"
        active = config_ui.ConfigHandler._active_request_count()
        assert active == 0


def test_llm_success_uses_exact_request_and_does_not_write_config(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"REMY_LLM_API_KEY": "saved-secret", "REMY_LANG": "en"})
    before = path.read_bytes()
    before_stat = path.stat()
    with _upstream() as (url, state), _server() as port:
        status, payload, _ = _request(port, "POST", "/api/test-llm", _test_payload(url))
    assert status == 200
    assert payload["category"] == "success"
    assert payload["http_status"] == 200
    assert payload["latency_ms"] >= 0
    assert len(state["requests"]) == 1
    upstream = state["requests"][0]
    assert upstream["headers"]["Authorization"] == "Bearer " + FAKE_SECRET
    request_json = json.loads(upstream["body"])
    assert set(request_json) == {"model", "messages", "max_tokens"}
    assert request_json["model"] == "fake-model"
    assert request_json["max_tokens"] == 1
    assert FAKE_SECRET not in upstream["body"].decode("utf-8")
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert FAKE_SECRET not in json.dumps(payload)


@pytest.mark.parametrize(("status_code", "category"), [
    (301, "redirect"), (302, "redirect"), (303, "redirect"),
    (307, "redirect"), (308, "redirect"), (401, "auth"),
    (403, "auth"), (404, "not_found"), (418, "request_rejected"),
    (429, "rate_limit"), (500, "server"), (503, "server"),
])
def test_llm_http_categories_and_no_retry(ui_home, status_code, category):
    _ = ui_home
    with _upstream(status=status_code, headers={"Location": "http://127.0.0.1:1/redirect"}) as (url, state), _server() as port:
        status, payload, _ = _request(port, "POST", "/api/test-llm", _test_payload(url))
    assert status == 200
    assert payload["category"] == category
    assert payload["http_status"] == status_code
    assert len(state["requests"]) == 1
    assert FAKE_SECRET not in json.dumps(payload)


@pytest.mark.parametrize("response_body", [
    b"", b"not-json", b"\xff", b"[]", b"{}",
    json.dumps({"choices": []}).encode("utf-8"),
    json.dumps({"choices": [{"message": "text"}]}).encode("utf-8"),
])
def test_llm_invalid_responses(ui_home, response_body):
    _ = ui_home
    with _upstream(payload=response_body) as (url, state), _server() as port:
        assert state["requests"] == []
        status, payload, _ = _request(port, "POST", "/api/test-llm", _test_payload(url))
    assert status == 200 and payload["category"] == "invalid_response"


def test_llm_response_size_boundary(ui_home):
    _ = ui_home
    valid = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")
    for target_size in (config_ui.MAX_RESPONSE_BYTES - 1, config_ui.MAX_RESPONSE_BYTES):
        padding = b" " * (target_size - len(valid))
        with _upstream(payload=valid + padding) as (url, state), _server() as port:
            status, payload, _ = _request(port, "POST", "/api/test-llm", _test_payload(url))
        assert status == 200 and payload["category"] == "success"
        assert len(state["requests"]) == 1
    with _upstream(payload=b"x" * (config_ui.MAX_RESPONSE_BYTES + 1)) as (url, state), _server() as port:
        status, payload, _ = _request(port, "POST", "/api/test-llm", _test_payload(url))
    assert status == 200 and payload["category"] == "too_large"
    assert len(state["requests"]) == 1


def test_llm_secret_actions_and_schema_validation(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"REMY_LLM_API_KEY": "saved-secret"})
    with _upstream() as (url, state), _server() as port:
        preserve = _test_payload(url, action="preserve", api_key="")
        status, payload, _ = _request(port, "POST", "/api/test-llm", preserve)
        assert status == 200 and payload["category"] == "success"
        assert state["requests"][0]["headers"]["Authorization"] == "Bearer saved-secret"
        clear = _test_payload(url, action="clear", api_key="")
        _, cleared, _ = _request(port, "POST", "/api/test-llm", clear)
        assert cleared["category"] == "missing_config"
        invalid = dict(preserve, extra="x")
        invalid_status, _, _ = _request(port, "POST", "/api/test-llm", invalid)
        assert invalid_status == 400


@pytest.mark.parametrize("candidate_url", [
    "", "relative/path", "file:///tmp/value", "http:///missing-host",
    "http://user:pass@127.0.0.1/test", "http://127.0.0.1/test#fragment",
    "http://127.0.0.1:bad/test", "http://127.0.0.1:70000/test",
    "http:// example.com/test", " http://example.com/test", "http://example.com/test\n",
])
def test_llm_rejects_invalid_urls(ui_home, candidate_url):
    _ = ui_home
    result = config_ui._probe_llm(FAKE_SECRET, candidate_url, "model")
    assert result["category"] == "invalid_url"


def test_llm_network_error_classification(ui_home, monkeypatch):
    _ = ui_home

    class Opener:
        error = urllib.error.URLError(TimeoutError("timeout"))

        def open(self, request, timeout):
            _ = request, timeout
            raise self.error

    opener = Opener()

    def build_opener(*handlers):
        assert handlers
        return opener

    monkeypatch.setattr(config_ui.urllib.request, "build_opener", build_opener)
    endpoint = "https://example.invalid/v1/chat/completions"
    cases = [
        (urllib.error.URLError(TimeoutError("timeout")), "timeout"),
        (urllib.error.URLError(ssl.SSLError("certificate")), "tls"),
        (urllib.error.URLError(socket.gaierror("dns")), "connection"),
        (urllib.error.URLError(ConnectionRefusedError("refused")), "connection"),
    ]
    for error, category in cases:
        opener.error = error
        result = config_ui._probe_llm(FAKE_SECRET, endpoint, "model")
        assert result["category"] == category
        assert result["http_status"] is None


def _extract_js_function(html, name):
    start = html.index("function " + name + "(")
    brace = html.index("{", start)
    depth = 0
    for index in range(brace, len(html)):
        if html[index] == "{":
            depth += 1
        elif html[index] == "}":
            depth -= 1
            if depth == 0:
                return html[start:index + 1]
    raise AssertionError("unterminated JavaScript function: " + name)


def test_node_executes_payload_diff_save_and_llm_functions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    html_text = (REMY_SRC / "config_ui.html").read_text(encoding="utf-8")
    source = "\n".join([
        _extract_js_function(html_text, "buildPayload"),
        _extract_js_function(html_text, "calculateModifiedKeys"),
        _extract_js_function(html_text, "resolveSaveOutcome"),
        _extract_js_function(html_text, "buildTestPayload"),
        _extract_js_function(html_text, "resolveTestState"),
        _extract_js_function(html_text, "releaseTestPayload"),
    ])
    script = source + "\n" + r'''
const registry=[{key:"A",type:"text"},{key:"B",type:"text"},{key:"S",type:"password"}];
const states={A:{value:"changed"},B:{value:"inherited"},S:{value:"",clear:true,modified:false}};
const payload=buildPayload(registry,states,{A:true,S:true},{A:true,S:true},{},false);
const removalPayload=buildPayload(registry,states,{A:true,B:true},{},{B:true},false);
const projectPayload=buildPayload(registry,states,{A:true,B:true},{A:true},{B:true},true);
const changed=calculateModifiedKeys(registry,{A:"base",B:"same"},{A:"changed",B:"same"},{},{},{S:"clear"},{},false);
const reverted=calculateModifiedKeys(registry,{A:"base",B:"same"},{A:"base",B:"same"},{},{},{},{},false);
const removalChanged=calculateModifiedKeys(registry,{A:"base",B:"same"},{A:"base",B:"same"},{},{},{},{B:true},false);
const projectRemovalIgnored=calculateModifiedKeys(registry,{A:"base",B:"same"},{A:"base",B:"same"},{},{},{},{B:true},true);
const outcomes=[resolveSaveOutcome(false,false),resolveSaveOutcome(true,false),resolveSaveOutcome(true,true)];
const testPayload=buildTestPayload("replace","secret","https://example.invalid","model");
const released=releaseTestPayload(testPayload);
process.stdout.write(JSON.stringify({payload,removalPayload,projectPayload,changed,reverted,removalChanged,projectRemovalIgnored,outcomes,released,testStates:[resolveTestState({category:"success"}),resolveTestState({category:"auth"})]}));
'''
    completed = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {
        "payload": {"values": {"A": "changed"}, "clear_secrets": ["S"]},
        "removalPayload": {"values": {"A": "changed"}, "clear_secrets": [], "remove_keys": ["B"]},
        "projectPayload": {"values": {"A": "changed"}, "clear_secrets": []},
        "changed": {"A": True, "S": True},
        "reverted": {},
        "removalChanged": {"B": True},
        "projectRemovalIgnored": {},
        "outcomes": [
            {"state": "error", "canClose": False},
            {"state": "refresh_error", "canClose": False},
            {"state": "idle", "canClose": True},
        ],
        "released": {"api_key_action": "replace", "base_url": "https://example.invalid", "model": "model"},
        "testStates": ["success", "error"],
    }


def test_node_executes_search_state_and_restore_functions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    html_text = (REMY_SRC / "config_ui.html").read_text(encoding="utf-8")
    source = "\n".join([
        _extract_js_function(html_text, "normalizeQuery"),
        _extract_js_function(html_text, "paramMatchesQuery"),
        _extract_js_function(html_text, "resolveFieldState"),
        _extract_js_function(html_text, "resolveRestoreAction"),
    ])
    script = source + "\n" + r'''
const q=normalizeQuery("  Max\t\n  Workers  ");
const param={key:"REMY_LLM_MAX_WORKERS",label_en:"Concurrent Requests",label_zh:"并发请求数",desc_en:"Concurrent LLM request workers",desc_zh:"LLM并发请求线程数"};
const matches=[
  paramMatchesQuery(param,"LLM Service","LLM服务",normalizeQuery("CONCURRENT")),
  paramMatchesQuery(param,"LLM Service","LLM服务",normalizeQuery("并发请求")),
  paramMatchesQuery(param,"LLM Service","LLM服务",normalizeQuery("llm service")),
  paramMatchesQuery(param,"LLM Service","LLM服务",normalizeQuery("remy_llm_max")),
  paramMatchesQuery(param,"LLM Service","LLM服务",normalizeQuery("nomatch")),
  paramMatchesQuery(param,"LLM Service","LLM服务",""),
  paramMatchesQuery(param,"LLM Service","LLM服务",normalizeQuery("  Concurrent \t Requests ")),
];
const states=[
  resolveFieldState({unsaved:true,environment:true,projectOverride:true,explicit:true,differsFromDefault:true}),
  resolveFieldState({unsaved:false,environment:true,projectOverride:true,explicit:true,differsFromDefault:true}),
  resolveFieldState({unsaved:false,environment:false,projectOverride:true,explicit:true,differsFromDefault:false}),
  resolveFieldState({unsaved:false,environment:false,projectOverride:false,explicit:true,differsFromDefault:false}),
  resolveFieldState({unsaved:false,environment:false,projectOverride:false,explicit:true,differsFromDefault:true}),
  resolveFieldState({unsaved:false,environment:false,projectOverride:false,explicit:false,differsFromDefault:false}),
];
const restores=[
  resolveRestoreAction({secret:false},{projectMode:false,pendingRemoval:true,explicit:true,unsavedEdit:false}),
  resolveRestoreAction({secret:false},{projectMode:false,pendingRemoval:false,explicit:true,unsavedEdit:false}),
  resolveRestoreAction({secret:false},{projectMode:false,pendingRemoval:false,explicit:false,unsavedEdit:true}),
  resolveRestoreAction({secret:false},{projectMode:false,pendingRemoval:false,explicit:false,unsavedEdit:false}),
  resolveRestoreAction({secret:true},{projectMode:false,pendingRemoval:false,explicit:true,unsavedEdit:true}),
  resolveRestoreAction({secret:false},{projectMode:true,override:true}),
  resolveRestoreAction({secret:false},{projectMode:true,override:false}),
];
process.stdout.write(JSON.stringify({q,matches,states,restores}));
'''
    completed = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {
        "q": "max workers",
        "matches": [True, True, True, True, False, True, True],
        "states": ["unsaved", "environment", "project", "explicit_default", "deviated", "default"],
        "restores": ["undo_restore", "restore_default", "restore_default", None, None, "restore_inherit", None],
    }


def test_html_security_and_control_contract():
    html_text = (REMY_SRC / "config_ui.html").read_text(encoding="utf-8")
    assert '<script nonce="__REMY_CSP_NONCE__">' in html_text
    assert "var sessionToken=__REMY_SESSION_TOKEN__;" in html_text
    assert ' onclick="' not in html_text and ' onerror="' not in html_text
    assert 'document.getElementById("lang-btn").addEventListener("click",toggleLang)' in html_text
    assert 'document.getElementById("lang-btn").disabled=busy' in html_text
    assert 'if(isSaveBusy())return;' in html_text
    assert '.btn:disabled,.btn-lang:disabled,.btn-toggle:disabled' in html_text
    assert 'opacity:.45;cursor:not-allowed;pointer-events:none' in html_text
    assert 'loadConfig(true,false)' in html_text
    assert 'save("none").then(function(ok){if(ok)doShutdown()})' in html_text
    assert '@media(prefers-reduced-motion:reduce)' in html_text
    assert 'postJson("/api/test-llm",payload)' in html_text
    assert '<div id="remy-host">' in html_text
    assert '<div id="config-page">' in html_text
    assert html_text.index('id="exit-btn"') < html_text.index('<div id="config-page">')
    assert 'id="search-input"' in html_text
    assert 'id="group-nav"' in html_text
    assert 'id="group-select"' in html_text
    assert 'class="actionbar" id="actionbar"' in html_text
    assert '.actionbar{position:sticky' in html_text
    assert '@media(max-width:900px){.group-nav{display:none}#group-select{display:block}}' in html_text
    assert 'header.setAttribute("aria-expanded"' in html_text
    assert 'header.setAttribute("aria-controls",bodyId)' in html_text
    assert 'lbl.htmlFor="p-"+param.key' in html_text
    assert 'if(e.key==="Escape"&&searchRaw){e.preventDefault();clearSearch()}' in html_text
    assert 'payload.remove_keys=removeKeys' in html_text
    assert 'delete pendingRemovals[id.slice(2)]' in html_text
