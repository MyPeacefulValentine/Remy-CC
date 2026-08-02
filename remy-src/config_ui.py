#!/usr/bin/env python3
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import ClassVar, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remy_config

HEARTBEAT_INTERVAL = 1
HEARTBEAT_TIMEOUT = 3
STARTUP_GRACE = 30
LOCK_FILE = Path.home() / ".claude" / ".config_ui.lock"
PARAM_REGISTRY = remy_config.registry_for_ui()
GROUPS = list(remy_config.GROUPS)


def _is_pid_alive(pid):
    if pid <= 0:
        return False
    if sys.platform == "win32":
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


class ConfigHandler(http.server.BaseHTTPRequestHandler):
    timeout = 10
    html_path: ClassVar[Optional[Path]] = None
    server_ref: ClassVar[Optional[http.server.ThreadingHTTPServer]] = None
    mode: ClassVar[str] = "global"
    target_path: ClassVar[Optional[Path]] = None
    project_root: ClassVar[Optional[Path]] = None
    last_heartbeat: ClassVar[float] = 0

    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            if self.html_path and self.html_path.exists():
                self._send_html(self.html_path.read_text(encoding="utf-8"))
            else:
                self._send_json({"error": "config_ui.html not found"}, 404)
            return
        if self.path == "/logo.svg":
            logo = Path(__file__).resolve().parent.parent / "remy-assets" / "logo.svg"
            if not logo.exists():
                self.send_error(404)
                return
            body = logo.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/heartbeat":
            ConfigHandler.last_heartbeat = time.monotonic()
            self._send_json({"status": "ok"})
            return
        if self.path != "/api/config":
            self.send_error(404)
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
        reset = bool(data.get("reset", False))
        project = self.mode == "project"
        target = self.target_path if project else remy_config.user_config_path()
        if target is None:
            raise remy_config.ConfigError("Project target is not configured")
        inspected = remy_config.inspect_document(target, project=project)
        if inspected["exists"] and not inspected["valid"]:
            raise remy_config.ConfigError("Configuration is read-only until file errors are corrected")
        if reset:
            remy_config.reset_known_values(target, project=project)
        else:
            overrides = data.get("overrides", []) if project else None
            remove_keys = []
            if project:
                if not isinstance(overrides, list) or any(not isinstance(key, str) for key in overrides):
                    raise remy_config.ConfigError("overrides must be a string list")
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
        self._send_json({"status": "ok"})

    def do_POST(self):
        if self.path == "/api/save":
            try:
                length = int(self.headers.get("Content-Length", 0))
                self._save(json.loads(self.rfile.read(length)))
            except (json.JSONDecodeError, remy_config.ConfigError, OSError) as exc:
                self._send_json({"status": "error", "message": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"status": "error", "message": type(exc).__name__}, 500)
            return
        if self.path == "/api/shutdown":
            try:
                self.send_response(200)
                self.end_headers()
            except OSError:
                pass
            if self.server_ref:
                threading.Thread(target=self.server_ref.shutdown, daemon=True).start()
            return
        self.send_error(404)


def _heartbeat_watchdog(server):
    start_time = time.monotonic()
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        if ConfigHandler.last_heartbeat > 0:
            if time.monotonic() - ConfigHandler.last_heartbeat > HEARTBEAT_TIMEOUT:
                threading.Thread(target=server.shutdown, daemon=True).start()
                break
        elif time.monotonic() - start_time > STARTUP_GRACE:
            threading.Thread(target=server.shutdown, daemon=True).start()
            break


def main(mode="global", target_path=None):
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
    ConfigHandler.last_heartbeat = 0
    url = "http://127.0.0.1:{}".format(port)
    acquire_lock(url, mode, target_path)

    label = "Global" if mode == "global" else "Project"
    print("Remy Config UI ({}): {}".format(label, url))
    print("  Target: " + str(ConfigHandler.target_path or remy_config.user_config_path()))
    if sys.platform == "win32":
        print("  Security: Windows file access depends on inherited user-directory ACLs.")
    print("Press Ctrl+C to stop.\n")

    watchdog = threading.Thread(target=_heartbeat_watchdog, args=(server,), daemon=True)
    watchdog.start()
    threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        release_lock()
        server.server_close()


if __name__ == "__main__":
    main()
