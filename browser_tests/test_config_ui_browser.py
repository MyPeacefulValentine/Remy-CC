import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Page

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

FAKE_SECRET = "browser-test-secret-marker"


class _UpstreamHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")
    requests = []
    release_response: threading.Event | None = None

    def log_message(self, format, *args):
        _ = format, args

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append({"headers": dict(self.headers), "body": body})
        release_response = _UpstreamHandler.release_response
        if release_response is not None:
            release_response.wait(5)
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)


@pytest.fixture
def app_servers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home if cls else home))
    monkeypatch.setattr(config_ui.Path, "home", classmethod(lambda cls: home if cls else home))
    for key in remy_config.FIELD_SPECS:
        monkeypatch.delenv(key, raising=False)
    remy_config.save_config(
        home / ".claude" / "remy-config.json",
        {"REMY_LANG": "en"},
    )

    _UpstreamHandler.requests = []
    _UpstreamHandler.release_response = None
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    config_ui.ConfigHandler.html_path = REMY_SRC / "config_ui.html"
    config_ui.ConfigHandler.mode = "global"
    config_ui.ConfigHandler.target_path = None
    config_ui.ConfigHandler.project_root = None
    config_ui.ConfigHandler.session_token = "browser-session-token"
    config_ui.ConfigHandler.test_lock = threading.Lock()
    with config_ui.ConfigHandler.activity_lock:
        config_ui.ConfigHandler.active_requests = 0
    app = ThreadingHTTPServer(("127.0.0.1", 0), config_ui.ConfigHandler)
    app_port = app.server_address[1]
    config_ui.ConfigHandler.server_ref = app
    config_ui.ConfigHandler.expected_authority = f"127.0.0.1:{app_port}"
    config_ui.ConfigHandler.expected_origin = f"http://127.0.0.1:{app_port}"
    app_thread = threading.Thread(target=app.serve_forever, daemon=True)
    app_thread.start()

    yield {
        "app_url": f"http://127.0.0.1:{app_port}",
        "upstream_url": f"http://127.0.0.1:{upstream.server_address[1]}/v1/chat/completions",
    }

    app.shutdown()
    app.server_close()
    app_thread.join(5)
    upstream.shutdown()
    upstream.server_close()
    upstream_thread.join(5)


def test_global_connection_ui_preserves_draft_and_reports_success(page: Page, app_servers):
    unexpected_requests = []
    page.on("request", lambda request: unexpected_requests.append(request.url) if not request.url.startswith("http://127.0.0.1:") else None)
    page_errors = []
    console_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

    response = page.goto(app_servers["app_url"])
    assert response is not None
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    page.locator("#p-REMY_LLM_API_KEY").fill(FAKE_SECRET)
    page.locator("#p-REMY_LLM_BASE_URL").fill(app_servers["upstream_url"])
    page.locator("#p-REMY_LLM_MODEL").fill("browser-model")
    assert page.locator("#save-btn").is_enabled()

    page.locator("#llm-test-btn").click()
    page.locator("#llm-test-result.success").wait_for()
    assert page.locator("#llm-test-result").get_attribute("role") == "status"
    assert page.locator("#llm-test-result").get_attribute("aria-live") == "polite"
    assert "Connection succeeded" in page.locator("#llm-test-result").inner_text()
    assert page.locator("#p-REMY_LLM_API_KEY").input_value() == FAKE_SECRET
    assert page.locator("#p-REMY_LLM_BASE_URL").input_value() == app_servers["upstream_url"]
    assert page.locator("#p-REMY_LLM_MODEL").input_value() == "browser-model"
    assert page.locator("#save-btn").is_enabled()
    assert _UpstreamHandler.requests[0]["headers"]["Authorization"] == "Bearer " + FAKE_SECRET
    assert FAKE_SECRET not in _UpstreamHandler.requests[0]["body"].decode("utf-8")

    page.locator("#p-REMY_LLM_MODEL").fill("changed-model")
    assert page.locator("#llm-test-result").inner_text() == ""
    assert page_errors == []
    assert console_errors == []
    assert unexpected_requests == []


def test_error_state_uses_alert_and_keeps_draft(page: Page, app_servers):
    _UpstreamHandler.response_status = 401
    try:
        page.goto(app_servers["app_url"])
        page.locator("#p-REMY_LLM_API_KEY").fill(FAKE_SECRET)
        page.locator("#p-REMY_LLM_BASE_URL").fill(app_servers["upstream_url"])
        page.locator("#p-REMY_LLM_MODEL").fill("browser-model")
        page.locator("#llm-test-btn").click()
        result = page.locator("#llm-test-result.error")
        result.wait_for()
        assert result.get_attribute("role") == "alert"
        assert result.get_attribute("aria-live") == "polite"
        assert "Authentication failed" in result.inner_text()
        assert "HTTP 401" in result.inner_text()
        assert page.locator("#p-REMY_LLM_API_KEY").input_value() == FAKE_SECRET
    finally:
        _UpstreamHandler.response_status = 200


def test_project_mode_has_no_connection_test(page: Page, app_servers):
    config_ui.ConfigHandler.mode = "project"
    page.goto(app_servers["app_url"])
    assert page.locator("#llm-test-btn").count() == 0


def test_reduced_motion_disables_spinner_animation(page: Page, app_servers):
    page.emulate_media(reduced_motion="reduce")
    page.goto(app_servers["app_url"])
    page.locator("#p-REMY_LLM_API_KEY").fill(FAKE_SECRET)
    page.locator("#p-REMY_LLM_BASE_URL").fill(app_servers["upstream_url"])
    page.locator("#p-REMY_LLM_MODEL").fill("browser-model")
    release_response = threading.Event()
    _UpstreamHandler.release_response = release_response
    page.locator("#llm-test-btn").click()
    page.locator(".llm-test-spinner").wait_for()
    for selector in (
        "#save-btn", "#reset-btn", "#clear-all-btn", "#exit-btn", "#lang-btn",
        "#llm-test-btn", "#p-REMY_LLM_API_KEY", "#p-REMY_LLM_BASE_URL", "#p-REMY_LLM_MODEL",
    ):
        assert page.locator(selector).is_disabled()
    assert page.locator("#llm-test-result").get_attribute("role") == "status"
    assert page.locator("#llm-test-result").get_attribute("aria-live") == "polite"
    animation = page.locator(".llm-test-spinner").evaluate("element => getComputedStyle(element).animationName")
    assert animation == "none"
    release_response.set()
    page.locator("#llm-test-result.success").wait_for()
    _UpstreamHandler.release_response = None
