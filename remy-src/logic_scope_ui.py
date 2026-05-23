#!/usr/bin/env python3
import http.server
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

LOCK_FILE = Path.home() / ".claude" / ".logic_scope_ui.lock"
SELECTION_FILE = os.path.join(".claude", "logic_inject_selection.json")
CACHE_FILE = os.path.join(".claude", "logic_index.json")
CONFIG_FILE = os.path.join(".claude", "logic_index_config")
SETTINGS_LOCAL = os.path.join(".claude", "settings.local.json")
PROFILES_FILE = os.path.join(".claude", "logic_scope_profiles.json")
MAX_PROFILES = 20

HEARTBEAT_INTERVAL = 1
HEARTBEAT_TIMEOUT = 3
STARTUP_GRACE = 30


def _is_pid_alive(pid):
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import subprocess
        result = subprocess.run(
            ["tasklist", "/FI", "PID eq {}".format(pid), "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(url):
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if _is_pid_alive(data.get("pid", -1)):
                print("Error: Another Logic Scope UI instance is running at " + data.get("url", "unknown"))
                sys.exit(1)
        except (json.JSONDecodeError, OSError):
            pass
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "url": url}, f)


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _load_cache(cwd):
    path = os.path.join(cwd, CACHE_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _load_selection(cwd):
    path = os.path.join(cwd, SELECTION_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _load_layers_from_config(cwd):
    path = os.path.join(cwd, CONFIG_FILE)
    layers = []
    if not os.path.exists(path):
        return layers
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("@layer:") and "=" in line:
                    rest = line[len("@layer:"):]
                    name = rest.split("=", 1)[0].strip()
                    if name:
                        layers.append(name)
    except OSError:
        pass
    return layers


def _build_tree_data(cwd):
    cache = _load_cache(cwd)
    selection = _load_selection(cwd)
    layer_names = _load_layers_from_config(cwd)

    files = []
    layer_file_map = {}
    for path, data in cache.items():
        if path == "_meta":
            continue
        symbols = data.get("symbols", [])
        layer = data.get("layer", "Core")
        class_count = sum(1 for s in symbols if s.get("type") == "class")
        func_count = sum(1 for s in symbols if s.get("type") == "function")
        files.append({
            "path": path,
            "layer": layer,
            "classes": class_count,
            "functions": func_count,
        })
        layer_file_map.setdefault(layer, []).append(path)

    all_layer_names = []
    seen = set()
    for name in layer_names:
        if name not in seen:
            all_layer_names.append(name)
            seen.add(name)
    for name in sorted(layer_file_map.keys()):
        if name not in seen:
            all_layer_names.append(name)
            seen.add(name)

    layers_out = []
    for name in all_layer_names:
        members = layer_file_map.get(name, [])
        layers_out.append({"name": name, "files": members})

    selected = None
    known = None
    if selection:
        selected = selection.get("selected_files")
        known = selection.get("known_files")

    return {
        "files": files,
        "layers": layers_out,
        "selected_files": selected,
        "known_files": known,
    }


def _save_selection(cwd, selected_files, known_files):
    from datetime import datetime, timezone
    path = os.path.join(cwd, SELECTION_FILE)
    data = {
        "version": "1.0.0",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "include",
        "selected_files": selected_files,
        "known_files": known_files,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, path)


def _load_profiles(cwd):
    path = os.path.join(cwd, PROFILES_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("profiles", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_profiles(cwd, profiles):
    path = os.path.join(cwd, PROFILES_FILE)
    data = {"version": "1.0.0", "max_profiles": MAX_PROFILES, "profiles": profiles}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, path)


def _disable_popup(cwd):
    path = os.path.join(cwd, SETTINGS_LOCAL)
    settings = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
    env = settings.setdefault("env", {})
    env["LOGIC_INDEX_INTERACTIVE"] = "false"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _get_lang(cwd):
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                return json.load(f).get("env", {}).get("REMY_LANG", "en")
        except (json.JSONDecodeError, OSError):
            pass
    local_path = os.path.join(cwd, SETTINGS_LOCAL)
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                return json.load(f).get("env", {}).get("REMY_LANG", "en")
        except (json.JSONDecodeError, OSError):
            pass
    return "en"


class ScopeHandler(http.server.BaseHTTPRequestHandler):
    timeout = 10
    cwd = None
    server_ref = None
    html_path = None
    last_heartbeat = 0
    session_timeout = 0

    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            if self.html_path and self.html_path.exists():
                self._send_html(self.html_path.read_text(encoding="utf-8"))
            else:
                self._send_json({"error": "logic_scope_ui.html not found"}, 404)
        elif self.path == "/api/tree-data":
            tree_data = _build_tree_data(self.cwd)
            tree_data["lang"] = _get_lang(self.cwd)
            tree_data["session_timeout"] = ScopeHandler.session_timeout
            self._send_json(tree_data)
        elif self.path == "/api/profiles":
            profiles = _load_profiles(self.cwd)
            self._send_json({"profiles": profiles, "max": MAX_PROFILES})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if self.path == "/api/save":
            try:
                data = json.loads(body) if body else {}
                selected = data.get("selected_files", [])
                known = data.get("known_files", [])
                _save_selection(self.cwd, selected, known)
                self._send_json({"status": "ok"})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)

        elif self.path == "/api/heartbeat":
            ScopeHandler.last_heartbeat = time.monotonic()
            self._send_json({"status": "ok"})

        elif self.path == "/api/shutdown":
            try:
                self.send_response(200)
                self.end_headers()
            except Exception:
                pass
            if self.server_ref:
                threading.Thread(target=self.server_ref.shutdown, daemon=True).start()

        elif self.path == "/api/disable-popup":
            try:
                data = json.loads(body) if body else {}
                selected = data.get("selected_files", [])
                known = data.get("known_files", [])
                _save_selection(self.cwd, selected, known)
                _disable_popup(self.cwd)
                self._send_json({"status": "ok"})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)

        elif self.path == "/api/profiles":
            try:
                data = json.loads(body) if body else {}
                action = data.get("action", "")
                profiles = _load_profiles(self.cwd)

                if action == "create":
                    name = data.get("name", "").strip()
                    if not name:
                        self._send_json({"status": "error", "message": "Name required"}, 400)
                        return
                    if any(p["name"] == name for p in profiles):
                        self._send_json({"status": "error", "message": "Name exists"}, 409)
                        return
                    if len(profiles) >= MAX_PROFILES:
                        self._send_json({"status": "error", "message": "Max profiles reached"}, 400)
                        return
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc).isoformat()
                    profile = {
                        "id": "p_" + str(int(time.time() * 1000)),
                        "name": name,
                        "selected_files": data.get("selected_files", []),
                        "known_files": data.get("known_files", []),
                        "created_at": now,
                        "last_used_at": now,
                    }
                    profiles.insert(0, profile)
                    _save_profiles(self.cwd, profiles)
                    self._send_json({"status": "ok", "profile": profile})

                elif action == "rename":
                    pid = data.get("id", "")
                    new_name = data.get("name", "").strip()
                    if not new_name:
                        self._send_json({"status": "error", "message": "Name required"}, 400)
                        return
                    if any(p["name"] == new_name and p["id"] != pid for p in profiles):
                        self._send_json({"status": "error", "message": "Name exists"}, 409)
                        return
                    for p in profiles:
                        if p["id"] == pid:
                            p["name"] = new_name
                            break
                    _save_profiles(self.cwd, profiles)
                    self._send_json({"status": "ok"})

                elif action == "delete":
                    pid = data.get("id", "")
                    profiles = [p for p in profiles if p["id"] != pid]
                    _save_profiles(self.cwd, profiles)
                    self._send_json({"status": "ok"})

                elif action == "batch-delete":
                    ids = set(data.get("ids", []))
                    profiles = [p for p in profiles if p["id"] not in ids]
                    _save_profiles(self.cwd, profiles)
                    self._send_json({"status": "ok"})

                elif action == "load":
                    pid = data.get("id", "")
                    target = None
                    for p in profiles:
                        if p["id"] == pid:
                            target = p
                            break
                    if not target:
                        self._send_json({"status": "error", "message": "Not found"}, 404)
                        return
                    _save_selection(self.cwd, target["selected_files"], target["known_files"])
                    from datetime import datetime, timezone
                    target["last_used_at"] = datetime.now(timezone.utc).isoformat()
                    profiles.sort(key=lambda x: x.get("last_used_at", ""), reverse=True)
                    _save_profiles(self.cwd, profiles)
                    self._send_json({"status": "ok", "selected_files": target["selected_files"], "known_files": target["known_files"]})

                else:
                    self._send_json({"status": "error", "message": "Unknown action"}, 400)
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)

        else:
            self.send_error(404)


def _heartbeat_watchdog(server):
    start_time = time.monotonic()
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        if ScopeHandler.last_heartbeat > 0:
            elapsed = time.monotonic() - ScopeHandler.last_heartbeat
            if elapsed > HEARTBEAT_TIMEOUT:
                threading.Thread(target=server.shutdown, daemon=True).start()
                break
        else:
            if time.monotonic() - start_time > STARTUP_GRACE:
                threading.Thread(target=server.shutdown, daemon=True).start()
                break


def main(cwd, timeout=300):
    cwd = os.path.abspath(cwd)
    html_path = Path(__file__).resolve().parent / "logic_scope_ui.html"

    ScopeHandler.cwd = cwd
    ScopeHandler.html_path = html_path
    ScopeHandler.last_heartbeat = 0
    ScopeHandler.session_timeout = timeout

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ScopeHandler)
    port = server.server_address[1]
    ScopeHandler.server_ref = server

    url = "http://127.0.0.1:{}".format(port)
    acquire_lock(url)

    print("Logic Scope UI: {}".format(url))

    watchdog = threading.Thread(target=_heartbeat_watchdog, args=(server,), daemon=True)
    watchdog.start()

    threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        release_lock()
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--timeout", type=int, default=0)
    args = parser.parse_args()
    main(args.cwd, args.timeout)
