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
        "home": home,
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


def test_desktop_search_navigation_and_advanced_folding(page: Page, app_servers):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(app_servers["app_url"])

    assert page.locator("#group-nav").is_visible()
    assert not page.locator("#group-select").is_visible()
    assert page.locator('button.group-header[data-group="llm_api"]').get_attribute("aria-expanded") == "true"
    assert page.locator('button.group-header[data-group="system"]').get_attribute("aria-expanded") == "false"
    assert page.locator("#p-REMY_LLM_API_KEY").count() == 1
    assert page.locator("#p-REMY_LANG").count() == 0
    assert page.locator("#p-REMY_LLM_RETRY_LIMIT").count() == 0

    page.locator('.group[data-group="llm_api"] .btn-advanced').click()
    assert page.locator("#p-REMY_LLM_RETRY_LIMIT").count() == 1
    page.locator('.group[data-group="llm_api"] .btn-advanced').click()
    assert page.locator("#p-REMY_LLM_RETRY_LIMIT").count() == 0

    page.locator("#search-input").fill("Interface Language")
    assert page.locator("#p-REMY_LANG").count() == 1
    assert page.locator("#search-count").inner_text() != ""
    assert page.locator('button.group-header[data-group="system"]').is_disabled()
    assert page.locator("#expand-all-btn").is_disabled()

    page.locator("#search-input").fill("zzz-no-match-query")
    assert page.locator("#search-empty.show").count() == 1
    assert "zzz-no-match-query" in page.locator("#search-empty-msg").inner_text()
    page.locator("#search-clear-btn").click()
    assert page.locator("#search-empty.show").count() == 0
    assert page.evaluate("document.activeElement.id") == "search-input"
    assert page.locator("#p-REMY_LANG").count() == 0

    page.locator("#search-input").fill("timeline")
    assert page.locator("#p-REMY_TIMELINE_INJECT_MODE").count() == 1
    page.locator("#search-input").press("Escape")
    assert page.locator("#search-input").input_value() == ""
    assert page.evaluate("document.activeElement.id") == "search-input"

    page.locator('#group-nav button[data-group="system"]').click()
    assert page.locator('button.group-header[data-group="system"]').get_attribute("aria-expanded") == "true"
    assert page.locator("#p-REMY_LANG").count() == 1
    assert page.locator('button.group-header[data-group="llm_api"]').get_attribute("aria-expanded") == "true"


def test_mobile_viewport_uses_group_select(page: Page, app_servers):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(app_servers["app_url"])
    assert not page.locator("#group-nav").is_visible()
    assert page.locator("#group-select").is_visible()
    page.locator("#group-select").select_option("system")
    assert page.locator("#p-REMY_LANG").count() == 1
    assert page.locator('button.group-header[data-group="system"]').get_attribute("aria-expanded") == "true"


def test_single_field_restore_roundtrip(page: Page, app_servers):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(app_servers["app_url"])
    page.locator('#group-nav button[data-group="system"]').click()
    config_path = app_servers["home"] / ".claude" / "remy-config.json"

    field = page.locator('.param[data-key="REMY_LANG"]')
    assert field.locator(".state-tag").inner_text() == "Explicit default"
    restore = field.locator(".btn-restore")
    assert restore.count() == 1
    assert "Restore default" in restore.inner_text()
    restore.click()

    field = page.locator('.param[data-key="REMY_LANG"]')
    assert "Undo restore" in field.locator(".btn-restore").inner_text()
    assert field.locator(".state-tag").inner_text() == "Unsaved"
    assert page.locator("#save-btn").is_enabled()

    page.locator("#p-REMY_LANG").select_option("zh-CN")
    page.locator("#save-btn").click()
    page.locator("#status.success").wait_for()
    assert remy_config.read_document(config_path)["values"]["REMY_LANG"] == "zh-CN"

    field = page.locator('.param[data-key="REMY_LANG"]')
    assert field.locator(".state-tag").inner_text() == "Custom value"
    assert "Restore default" in field.locator(".btn-restore").inner_text()
    field.locator(".btn-restore").click()
    page.locator("#save-btn").click()
    page.wait_for_function(
        "() => !document.querySelector('.param[data-key=\"REMY_LANG\"] .btn-restore')"
    )

    assert "REMY_LANG" not in remy_config.read_document(config_path)["values"]
    field = page.locator('.param[data-key="REMY_LANG"]')
    assert field.locator(".btn-restore").count() == 0
    assert field.locator(".state-tag").inner_text() == "Default"


def test_hidden_modified_field_is_still_saved(page: Page, app_servers):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(app_servers["app_url"])
    page.locator('#group-nav button[data-group="system"]').click()
    page.locator("#p-REMY_LANG").select_option("zh-CN")
    page.locator("#search-input").fill("concurrent")
    assert page.locator("#p-REMY_LANG").count() == 0
    assert page.locator("#save-btn").is_enabled()
    page.locator("#save-btn").click()
    page.locator("#status.success").wait_for()
    values = remy_config.read_document(
        app_servers["home"] / ".claude" / "remy-config.json"
    )["values"]
    assert values["REMY_LANG"] == "zh-CN"
