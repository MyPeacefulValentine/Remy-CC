#!/usr/bin/env python3
import argparse
import hmac
import html
import http.server
import json
import os
import platform
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import ClassVar, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remy_config

HEARTBEAT_INTERVAL = 2
IDLE_POLL_INTERVAL = 5
LLM_TEST_TIMEOUT = 15
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
RESET_MODES = frozenset({"none", "non_secret", "all"})
TEST_SECRET_ACTIONS = frozenset({"preserve", "replace", "clear"})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
LOCK_FILE = Path.home() / ".claude" / ".config_ui.lock"
PARAM_REGISTRY = remy_config.registry_for_ui()
GROUPS = list(remy_config.GROUPS)
SESSION_TOKEN_PLACEHOLDER = "__REMY_SESSION_TOKEN__"
CSP_NONCE_PLACEHOLDER = "__REMY_CSP_NONCE__"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _ = req, fp, code, msg, headers, newurl
        return None


def _is_pid_alive(pid):
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        result = subprocess.run(
            ["tasklist", "/FI", "PID eq {}".format(pid), "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(url, mode, target):
    if LOCK_FILE.exists():
        try:
            data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            if _is_pid_alive(data.get("pid", -1)):
                print("Error: Another config UI instance is running.")
                print("  URL:    " + data.get("url", "unknown"))
                print("  Mode:   " + data.get("mode", "unknown"))
                if data.get("target"):
                    print("  Target: " + data["target"])
                sys.exit(1)
        except (json.JSONDecodeError, OSError):
            pass
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(
        json.dumps({"pid": os.getpid(), "url": url, "mode": mode, "target": str(target or "")}),
        encoding="utf-8",
    )


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _document_values(path, project=False, strict=True):
    if not path.exists():
        return {}, [], ()
    if strict:
        document = remy_config.read_document(path, strict=True, project=project)
        diagnostics = ()
    else:
        inspected = remy_config.inspect_document(path, project=project)
        document = {"values": inspected["values"]}
        diagnostics = inspected["diagnostics"]
    values = document["values"]
    unknown = sorted(key for key in values if key not in remy_config.FIELD_SPECS)
    return values, unknown, diagnostics


def _safe_values(values):
    return {
        key: value
        for key, value in values.items()
        if key not in remy_config.SECRET_KEYS
    }


def _test_result(category, start_time, http_status=None):
    return {
        "status": "ok" if category == "success" else "error",
        "category": category,
        "http_status": http_status,
        "latency_ms": max(0, int(round((time.monotonic() - start_time) * 1000))),
    }


def _classify_network_error(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    return "connection"


def _valid_endpoint_url(base_url):
    if not base_url or base_url != base_url.strip() or any(character.isspace() for character in base_url):
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in base_url):
        return False
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


def _probe_llm(api_key, base_url, model):
    start_time = time.monotonic()
    if not api_key:
        return _test_result("missing_config", start_time)
    if not _valid_endpoint_url(base_url):
        return _test_result("invalid_url", start_time)
    if not model.strip():
        return _test_result("missing_config", start_time)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply with a short text response."},
            {"role": "user", "content": "Connection test."},
        ],
        "max_tokens": 1,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "Remy-CC-Config-UI",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=LLM_TEST_TIMEOUT) as response:
            http_status = response.getcode()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return _test_result("too_large", start_time, http_status)
        try:
            result = json.loads(raw.decode("utf-8"))
            message = result["choices"][0]["message"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            return _test_result("invalid_response", start_time, http_status)
        if not isinstance(message, dict):
            return _test_result("invalid_response", start_time, http_status)
        return _test_result("success", start_time, http_status)
    except urllib.error.HTTPError as error:
        if error.code in REDIRECT_CODES:
            category = "redirect"
        elif error.code in (401, 403):
            category = "auth"
        elif error.code == 404:
            category = "not_found"
        elif error.code == 429:
            category = "rate_limit"
        elif 500 <= error.code <= 599:
            category = "server"
        elif 400 <= error.code <= 499:
            category = "request_rejected"
        else:
            category = "invalid_response"
        return _test_result(category, start_time, error.code)
    except ssl.SSLError:
        return _test_result("tls", start_time)
    except (socket.timeout, TimeoutError):
        return _test_result("timeout", start_time)
    except (urllib.error.URLError, ConnectionError, OSError) as error:
        return _test_result(_classify_network_error(error), start_time)
    finally:
        payload = None
        body = None
        request = None


class ConfigHandler(http.server.BaseHTTPRequestHandler):
    timeout = 10
    html_path: ClassVar[Optional[Path]] = None
    server_ref: ClassVar[Optional[http.server.ThreadingHTTPServer]] = None
    mode: ClassVar[str] = "global"
    target_path: ClassVar[Optional[Path]] = None
    project_root: ClassVar[Optional[Path]] = None
    expected_authority: ClassVar[str] = ""
    expected_origin: ClassVar[str] = ""
    session_token: ClassVar[str] = secrets.token_urlsafe(32)
    active_requests: ClassVar[int] = 0
    activity_lock: ClassVar[threading.Lock] = threading.Lock()
    test_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _begin_request(cls):
        with cls.activity_lock:
            cls.active_requests += 1

    @classmethod
    def _end_request(cls):
        with cls.activity_lock:
            cls.active_requests = max(0, cls.active_requests - 1)

    @classmethod
    def _active_request_count(cls):
        with cls.activity_lock:
            return cls.active_requests

    def log_message(self, format, *args):
        _ = format, args

    def _send_security_headers(self, *, dynamic=True, csp=None):
        self.send_header("X-Content-Type-Options", "nosniff")
        if dynamic:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
        if csp is not None:
            self.send_header("Content-Security-Policy", csp)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content, status=200):
        nonce = secrets.token_urlsafe(16)
        rendered = content.replace(
            CSP_NONCE_PLACEHOLDER,
            html.escape(nonce, quote=True),
        ).replace(
            SESSION_TOKEN_PLACEHOLDER,
            json.dumps(self.session_token),
        )
        body = rendered.encode("utf-8")
        csp = (
            "default-src 'none'; "
            "script-src 'nonce-{}'; "
            "style-src 'unsafe-inline'; "
            "img-src 'self'; "
            "connect-src 'self'; "
            "base-uri 'none'; "
            "form-action 'none'; "
            "frame-ancestors 'none'"
        ).format(nonce)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(csp=csp)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        _ = message, explain
        self._send_json({"status": "error", "message": "request_error"}, code)

    def _valid_post_source(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        supplied = self.headers.get("X-Remy-Session", "")
        try:
            token_matches = hmac.compare_digest(
                supplied.encode("ascii"),
                self.session_token.encode("ascii"),
            )
        except UnicodeEncodeError:
            token_matches = False
        return (
            host == self.expected_authority
            and origin == self.expected_origin
            and token_matches
        )

    def _read_json_body(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json({"status": "error", "message": "unsupported_media_type"}, 415)
            return None
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._send_json({"status": "error", "message": "invalid_content_length"}, 400)
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._send_json({"status": "error", "message": "incomplete_request"}, 400)
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"status": "error", "message": "invalid_json"}, 400)
            return None

    def do_GET(self):
        if self.path == "/api/heartbeat" and _managed_state is not None:
            with _managed_state.lock:
                _managed_state.last_heartbeat = time.monotonic()
        if self.path == "/":
            if self.html_path and self.html_path.exists():
                self._send_html(self.html_path.read_text(encoding="utf-8"))
            else:
                self._send_json({"status": "error", "message": "config_ui_missing"}, 404)
            return
        if self.path == "/logo.svg":
            logo = Path(__file__).resolve().parent.parent / "remy-assets" / "logo.svg"
            if not logo.exists():
                self._send_json({"status": "error", "message": "logo_missing"}, 404)
                return
            body = logo.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers(dynamic=False)
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/heartbeat":
            self._send_json({"status": "ok"})
            return
        if self.path != "/api/config":
            self._send_json({"status": "error", "message": "not_found"}, 404)
            return
        snapshot = remy_config.load_config(self.project_root, strict=False)
        user_values, user_unknown, user_diagnostics = _document_values(
            remy_config.user_config_path(), strict=False
        )
        project_values = {}
        project_unknown = []
        project_diagnostics = ()
        if self.mode == "project" and self.target_path:
            project_values, project_unknown, project_diagnostics = _document_values(
                self.target_path, project=True, strict=False
            )
        diagnostics = tuple(dict.fromkeys(
            (*snapshot.diagnostics, *user_diagnostics, *project_diagnostics)
        ))
        secret_state = {
            key: {
                "has_value": bool(snapshot.get(key)),
                "source": snapshot.source_of(key),
            }
            for key in remy_config.SECRET_KEYS
        }
        self._send_json({
            "mode": self.mode,
            "target": str(self.target_path or remy_config.user_config_path()),
            "values": _safe_values(snapshot.raw_values),
            "sources": dict(snapshot.sources),
            "user_values": _safe_values(user_values),
            "project_values": _safe_values(project_values),
            "secret_state": secret_state,
            "lang": str(snapshot.get("REMY_LANG", "en")),
            "registry": PARAM_REGISTRY,
            "groups": GROUPS,
            "unknown_keys": sorted(set(user_unknown + project_unknown)),
            "diagnostics": list(diagnostics),
            "read_only": bool(diagnostics),
            "windows_acl_warning": sys.platform == "win32",
        })

    def _save(self, data):
        if not isinstance(data, dict):
            raise remy_config.ConfigError("Save payload must be an object")
        updates = data.get("values", {})
        if not isinstance(updates, dict):
            raise remy_config.ConfigError("values must be an object")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in updates.items()):
            raise remy_config.ConfigError("Configuration values must be strings")
        clear_secrets = data.get("clear_secrets", [])
        if not isinstance(clear_secrets, list) or any(not isinstance(key, str) for key in clear_secrets):
            raise remy_config.ConfigError("clear_secrets must be a string list")
        remove_requested = data.get("remove_keys", [])
        if not isinstance(remove_requested, list) or any(not isinstance(key, str) for key in remove_requested):
            raise remy_config.ConfigError("remove_keys must be a string list")
        if len(set(remove_requested)) != len(remove_requested):
            raise remy_config.ConfigError("remove_keys must not contain duplicates")
        for key in remove_requested:
            if key not in remy_config.FIELD_SPECS:
                raise remy_config.ConfigError("Unknown Remy configuration field in remove_keys: " + key)
            if key in remy_config.SECRET_KEYS:
                raise remy_config.ConfigError(key + " cannot be removed through remove_keys")
        if "reset" in data:
            raise remy_config.ConfigError("reset is unsupported; use reset_mode")
        reset_mode = data.get("reset_mode", "none")
        if not isinstance(reset_mode, str) or reset_mode not in RESET_MODES:
            raise remy_config.ConfigError("reset_mode must be one of none, non_secret, all")
        project = self.mode == "project"
        if project and remove_requested:
            raise remy_config.ConfigError("remove_keys is unavailable for project configuration")
        overrides = data.get("overrides", []) if project else []
        if not isinstance(overrides, list) or any(not isinstance(key, str) for key in overrides):
            raise remy_config.ConfigError("overrides must be a string list")
        if reset_mode != "none" and (updates or clear_secrets or overrides or remove_requested):
            raise remy_config.ConfigError("reset_mode cannot be combined with values, clear_secrets, remove_keys, or overrides")
        if project and reset_mode == "non_secret":
            raise remy_config.ConfigError("non_secret reset is unavailable for project configuration")
        target = self.target_path if project else remy_config.user_config_path()
        if target is None:
            raise remy_config.ConfigError("Project target is not configured")
        inspected = remy_config.inspect_document(target, project=project)
        if inspected["exists"] and not inspected["valid"]:
            raise remy_config.ConfigError("Configuration is read-only until file errors are corrected")
        if reset_mode == "non_secret":
            remy_config.reset_non_secret_values(target, project=False)
        elif reset_mode == "all":
            remy_config.reset_known_values(target, project=project)
        else:
            remove_keys = list(remove_requested)
            if project:
                allowed = set(overrides)
                updates = {key: value for key, value in updates.items() if key in allowed}
                current, _, _ = _document_values(target, project=True)
                remove_keys = [
                    key for key in remy_config.FIELD_SPECS
                    if key in current and key not in allowed
                ]
            remy_config.save_config(
                target,
                updates,
                remove_keys=remove_keys,
                clear_secrets=clear_secrets,
                project=project,
            )
        snapshot = remy_config.load_config(self.project_root, strict=True)
        if not project:
            claude_home = remy_config.user_config_path().parent
            lang = str(snapshot.get("REMY_LANG", "en"))
            patch_script = claude_home / "remy-src" / "patch_descriptions.py"
            if patch_script.exists():
                subprocess.run(
                    [sys.executable, str(patch_script), "--claude-home", str(claude_home), "--lang", lang],
                    check=False,
                )
        restart_pending = sorted(
            key for key in updates
            if key in remy_config.FIELD_SPECS
            and remy_config.FIELD_SPECS[key].restart_scope != "immediate"
        ) if reset_mode == "none" else []
        self._send_json({"status": "ok", "restart_pending": restart_pending})

    def _test_llm(self, data):
        if self.mode != "global":
            raise remy_config.ConfigError("LLM connection testing is only available in global mode")
        if not isinstance(data, dict):
            raise remy_config.ConfigError("Test payload must be an object")
        expected = {"api_key_action", "api_key", "base_url", "model"}
        if set(data) != expected:
            raise remy_config.ConfigError("Test payload fields are invalid")
        if any(not isinstance(data[key], str) for key in expected):
            raise remy_config.ConfigError("Test payload values must be strings")
        action = data["api_key_action"]
        api_key = data["api_key"]
        if action not in TEST_SECRET_ACTIONS:
            raise remy_config.ConfigError("api_key_action is invalid")
        if action == "replace" and not api_key:
            raise remy_config.ConfigError("Replacement API key must not be empty")
        if action != "replace" and api_key:
            raise remy_config.ConfigError("API key must be empty unless replacing")
        if not data["base_url"]:
            return _test_result("invalid_url", time.monotonic())
        if not data["model"].strip():
            return _test_result("missing_config", time.monotonic())
        if action == "preserve":
            api_key = str(remy_config.load_config(self.project_root, strict=True).get("REMY_LLM_API_KEY", ""))
        elif action == "clear":
            api_key = ""
        return _probe_llm(api_key, data["base_url"], data["model"])

    def do_POST(self):
        self.close_connection = True
        if not self._valid_post_source():
            self._send_json({"status": "error", "message": "forbidden"}, 403)
            return
        data = self._read_json_body()
        if data is None:
            return
        if self.path == "/api/save":
            ConfigHandler._begin_request()
            try:
                self._save(data)
            except (remy_config.ConfigError, OSError) as exc:
                self._send_json({"status": "error", "message": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"status": "error", "message": type(exc).__name__}, 500)
            finally:
                ConfigHandler._end_request()
            return
        if self.path == "/api/test-llm":
            if not ConfigHandler.test_lock.acquire(blocking=False):
                self._send_json(_test_result("busy", time.monotonic()), 409)
                return
            ConfigHandler._begin_request()
            try:
                self._send_json(self._test_llm(data))
            except remy_config.ConfigError as exc:
                self._send_json({"status": "error", "message": str(exc)}, 400)
            except Exception:
                self._send_json({"status": "error", "message": "internal_error"}, 500)
            finally:
                ConfigHandler._end_request()
                ConfigHandler.test_lock.release()
            return
        if self.path == "/api/shutdown":
            if data != {}:
                self._send_json({"status": "error", "message": "shutdown_payload_must_be_empty"}, 400)
                return
            self._send_json({"status": "ok"})
            if self.server_ref:
                threading.Thread(target=self.server_ref.shutdown, daemon=True).start()
            return
        self._send_json({"status": "error", "message": "not_found"}, 404)


class _ManagedState:
    """Managed-arm runtime state: heartbeat clock and shutdown trigger."""

    def __init__(self, idle_timeout):
        self.lock = threading.Lock()
        self.last_heartbeat = time.monotonic()
        self.idle_timeout = idle_timeout


_managed_state: Optional[_ManagedState] = None


def _managed_idle_timeout():
    snapshot = remy_config.load_config(None, strict=False)
    value = snapshot.get("REMY_CONFIG_UI_IDLE_TIMEOUT", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _watch_stdin_eof(server):
    try:
        while sys.stdin.buffer.read(1):
            pass
    except (OSError, ValueError):
        pass
    threading.Thread(target=server.shutdown, daemon=True).start()


def _watch_idle(state, server):
    while True:
        time.sleep(IDLE_POLL_INTERVAL)
        with state.lock:
            idle_for = time.monotonic() - state.last_heartbeat
        if idle_for >= state.idle_timeout:
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def _start_managed_watchdogs(state, server):
    threading.Thread(target=_watch_stdin_eof, args=(server,), daemon=True).start()
    if state.idle_timeout > 0:
        threading.Thread(target=_watch_idle, args=(state, server), daemon=True).start()


def main(mode="global", target_path=None, managed=False):
    global _managed_state
    ConfigHandler.html_path = Path(__file__).resolve().parent / "config_ui.html"
    ConfigHandler.mode = mode
    if mode == "project" and target_path:
        project_dir = Path(target_path).resolve()
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        ConfigHandler.project_root = project_dir
        config_path = claude_dir / remy_config.CONFIG_FILE_NAME
        ConfigHandler.target_path = config_path
        remy_config.migrate_settings_file(
            claude_dir / "settings.local.json",
            config_path,
            project=True,
        )
    else:
        ConfigHandler.project_root = None
        ConfigHandler.target_path = None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ConfigHandler)
    port = server.server_address[1]
    ConfigHandler.server_ref = server
    ConfigHandler.expected_authority = "127.0.0.1:{}".format(port)
    ConfigHandler.expected_origin = "http://" + ConfigHandler.expected_authority
    ConfigHandler.session_token = secrets.token_urlsafe(32)
    ConfigHandler.test_lock = threading.Lock()
    with ConfigHandler.activity_lock:
        ConfigHandler.active_requests = 0
    url = ConfigHandler.expected_origin
    acquire_lock(url, mode, target_path)

    if managed:
        _managed_state = _ManagedState(_managed_idle_timeout())
        _start_managed_watchdogs(_managed_state, server)
        report = json.dumps({"port": port, "token": ConfigHandler.session_token, "pid": os.getpid()})
        print(report, flush=True)
    else:
        label = "Global" if mode == "global" else "Project"
        print("Remy Config UI ({}): {}".format(label, url))
        print("  Target: " + str(ConfigHandler.target_path or remy_config.user_config_path()))
        if platform.system() == "Windows":
            print("  Security: Windows file access depends on inherited user-directory ACLs.")
        print("Press the page Exit button or Ctrl+C to stop.\n")
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if not managed:
            print("\nStopped.")
    finally:
        release_lock()
        server.server_close()
        _managed_state = None


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="config_ui")
    parser.add_argument("--managed", action="store_true")
    parser.add_argument("--mode", choices=["global", "project"], default="global")
    parser.add_argument("--target", default=None)
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    main(mode=args.mode, target_path=args.target, managed=args.managed)
