import contextlib
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
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
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(config_ui.Path, "home", classmethod(lambda cls: home))
    for key in remy_config.FIELD_SPECS:
        monkeypatch.delenv(key, raising=False)
    config_ui.ConfigHandler.mode = "global"
    config_ui.ConfigHandler.target_path = None
    config_ui.ConfigHandler.project_root = None
    return home


@contextlib.contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), config_ui.ConfigHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _request(port, method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json", "Content-Length": str(len(body))}
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, data


def test_get_redacts_secret_and_reports_source(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"REMY_LLM_API_KEY": "fake-secret", "REMY_LANG": "zh-CN"})
    with _server() as port:
        status, payload = _request(port, "GET", "/api/config")
    encoded = json.dumps(payload)
    assert status == 200
    assert "fake-secret" not in encoded
    assert payload["secret_state"]["REMY_LLM_API_KEY"] == {"has_value": True, "source": "user"}
    assert payload["sources"]["REMY_LANG"] == "user"
    assert payload["read_only"] is False


def test_sparse_save_preserves_unknown_and_secret(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"FUTURE_FIELD": "keep", "REMY_LLM_API_KEY": "fake-secret"})
    with _server() as port:
        status, payload = _request(port, "POST", "/api/save", {"values": {"REMY_LANG": "zh-CN"}, "clear_secrets": []})
    values = remy_config.read_document(path)["values"]
    assert status == 200 and payload["status"] == "ok"
    assert values == {"FUTURE_FIELD": "keep", "REMY_LLM_API_KEY": "fake-secret", "REMY_LANG": "zh-CN"}


def test_replace_and_clear_secret(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"REMY_LLM_API_KEY": "first"})
    with _server() as port:
        assert _request(port, "POST", "/api/save", {"values": {"REMY_LLM_API_KEY": "second"}})[0] == 200
        assert remy_config.read_document(path)["values"]["REMY_LLM_API_KEY"] == "second"
        assert _request(port, "POST", "/api/save", {"values": {}, "clear_secrets": ["REMY_LLM_API_KEY"]})[0] == 200
    assert "REMY_LLM_API_KEY" not in remy_config.read_document(path)["values"]


def test_project_rejects_secret_and_removes_disabled_override(ui_home, tmp_path):
    project = tmp_path / "project"
    target = project / ".claude" / "remy-config.json"
    _write(target, {"REMY_LANG": "zh-CN", "REMY_LLM_MAX_WORKERS": "4"})
    config_ui.ConfigHandler.mode = "project"
    config_ui.ConfigHandler.project_root = project
    config_ui.ConfigHandler.target_path = target
    with _server() as port:
        status, _ = _request(port, "POST", "/api/save", {
            "values": {"REMY_LANG": "en", "REMY_LLM_API_KEY": "fake-secret"},
            "overrides": ["REMY_LANG", "REMY_LLM_API_KEY"],
        })
        assert status == 400
        status, payload = _request(port, "POST", "/api/save", {
            "values": {"REMY_LANG": "en"}, "overrides": ["REMY_LANG"]
        })
    assert status == 200 and payload["status"] == "ok"
    assert remy_config.read_document(target, project=True)["values"] == {"REMY_LANG": "en"}


def test_reset_preserves_unknown_keys(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    _write(path, {"FUTURE_FIELD": "keep", "REMY_LANG": "zh-CN"})
    with _server() as port:
        status, _ = _request(port, "POST", "/api/save", {"values": {}, "reset": True})
    assert status == 200
    assert remy_config.read_document(path)["values"] == {"FUTURE_FIELD": "keep"}


def test_invalid_file_is_read_only_and_save_is_rejected(ui_home):
    path = ui_home / ".claude" / "remy-config.json"
    path.parent.mkdir(parents=True)
    original = b"{broken"
    path.write_bytes(original)
    with _server() as port:
        status, payload = _request(port, "GET", "/api/config")
        save_status, save_payload = _request(port, "POST", "/api/save", {"values": {"REMY_LANG": "zh-CN"}})
    assert status == 200
    assert payload["read_only"] is True
    assert payload["diagnostics"]
    assert save_status == 400 and save_payload["status"] == "error"
    assert path.read_bytes() == original


def test_node_executes_sparse_payload_function():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    html = (REMY_SRC / "config_ui.html").read_text(encoding="utf-8")
    start = html.index("function buildPayload(")
    end = html.index("\nif(typeof globalThis", start)
    source = html[start:end]
    script = source + "\n" + r'''
const registry=[{key:"A",type:"text"},{key:"B",type:"text"},{key:"S",type:"password"}];
const states={A:{value:"changed"},B:{value:"inherited"},S:{value:"",clear:true,modified:false}};
const out=buildPayload(registry,states,{A:true,S:true},{A:true,S:true},false);
process.stdout.write(JSON.stringify(out));
'''
    completed = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == {"values": {"A": "changed"}, "clear_secrets": ["S"]}
