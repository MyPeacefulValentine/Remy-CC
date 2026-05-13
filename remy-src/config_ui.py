#!/usr/bin/env python3
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

PARAM_REGISTRY = [
    {"key": "LOGIC_INDEX_FILTER_SMALL", "group": "llm_api", "type": "enum", "default": "false",
     "options": ["true", "false"],
     "option_desc_en": ["Skip small functions (< 3 lines, no docstring)", "Summarize all functions"],
     "option_desc_zh": ["跳过小函数（< 3 行且无文档）", "为所有函数生成摘要"],
     "desc_en": "Skip LLM summarization for small functions without docstrings",
     "desc_zh": "跳过无文档小函数的 LLM 摘要生成"},
    {"key": "OPENAI_API_KEY", "group": "llm_api", "type": "password", "default": "",
     "desc_en": "API key for OpenAI-compatible LLM service",
     "desc_zh": "OpenAI 兼容 LLM 服务的 API Key"},
    {"key": "OPENAI_BASE_URL", "group": "llm_api", "type": "url",
     "default": "https://api.deepseek.com/v1/chat/completions",
     "desc_en": "API endpoint URL",
     "desc_zh": "API 端点 URL"},
    {"key": "OPENAI_MODEL", "group": "llm_api", "type": "text", "default": "deepseek-v4-flash",
     "desc_en": "Model name (e.g., deepseek-v4-flash, glm-5)",
     "desc_zh": "模型名称（如 deepseek-v4-flash、glm-5）"},
    {"key": "OPENAI_MAX_WORKERS", "group": "llm_api", "type": "int", "default": "3",
     "min": 1, "max": 10,
     "desc_en": "Concurrent API request threads",
     "desc_zh": "并发 API 请求线程数"},
    {"key": "OPENAI_RETRY_LIMIT", "group": "llm_api", "type": "int", "default": "3",
     "min": 0, "max": 10,
     "desc_en": "Max retry attempts on API failure",
     "desc_zh": "API 失败时的最大重试次数"},
    {"key": "OPENAI_TIMEOUT", "group": "llm_api", "type": "int", "default": "300",
     "min": 30, "max": 600,
     "desc_en": "API request timeout in seconds",
     "desc_zh": "API 请求超时（秒）"},
    {"key": "OPENAI_MAX_TOKENS", "group": "llm_api", "type": "int", "default": "8192",
     "min": 1024, "max": 32768,
     "desc_en": "Max tokens in API response",
     "desc_zh": "API 响应最大 token 数"},

    {"key": "LOGIC_INDEX_AUTO_INJECT", "group": "injection", "type": "enum", "default": "ALWAYS",
     "options": ["ALWAYS", "ASK", "NEVER"],
     "option_desc_en": ["Auto-inject on every update", "Prompt before injection", "Generate file only, never inject"],
     "option_desc_zh": ["每次更新自动注入", "注入前询问确认", "仅生成文件，不注入"],
     "desc_en": "Auto-inject logic_tree.md into CLAUDE.md",
     "desc_zh": "自动将 logic_tree.md 注入 CLAUDE.md"},

    {"key": "IMPACT_DEPTH_UP", "group": "impact", "type": "int", "default": "2",
     "min": 1, "max": 10,
     "desc_en": "Upstream (callers) BFS search depth",
     "desc_zh": "上游（调用者）BFS 搜索深度"},
    {"key": "IMPACT_DEPTH_DOWN", "group": "impact", "type": "int", "default": "2",
     "min": 1, "max": 10,
     "desc_en": "Downstream (callees) BFS search depth",
     "desc_zh": "下游（被调用者）BFS 搜索深度"},

    {"key": "PROJECT_TREE_AUTO_INJECT", "group": "injection", "type": "enum", "default": "ALWAYS",
     "options": ["ALWAYS", "ASK", "NEVER"],
     "option_desc_en": ["Auto-inject on every update", "Prompt before injection", "Generate file only, never inject"],
     "option_desc_zh": ["每次更新自动注入", "注入前询问确认", "仅生成文件，不注入"],
     "desc_en": "Auto-inject project_tree.md into CLAUDE.md",
     "desc_zh": "自动将 project_tree.md 注入 CLAUDE.md"},
    {"key": "TIMELINE_AUTO_INJECT", "group": "injection", "type": "enum", "default": "ALWAYS",
     "options": ["ALWAYS", "ASK", "NEVER"],
     "option_desc_en": ["Auto-inject on every update", "Prompt before injection", "Generate file only, never inject"],
     "option_desc_zh": ["每次更新自动注入", "注入前询问确认", "仅生成文件，不注入"],
     "desc_en": "Auto-inject timeline_view.md into CLAUDE.md",
     "desc_zh": "自动将 timeline_view.md 注入 CLAUDE.md"},

    {"key": "TIMELINE_INJECT_MODE", "group": "timeline", "type": "enum", "default": "all",
     "options": ["all", "last_n", "since_date", "within_days"],
     "option_desc_en": ["Show all records", "Keep latest N entries", "Keep entries after date (YYYY-MM-DD)", "Keep entries within N days"],
     "option_desc_zh": ["显示全部记录", "保留最新 N 条", "保留指定日期之后的记录", "保留最近 N 天内的记录"],
     "desc_en": "Timeline filter mode",
     "desc_zh": "时间线过滤模式"},
    {"key": "TIMELINE_INJECT_VALUE", "group": "timeline", "type": "text", "default": "",
     "desc_en": "Filter value (e.g., '10' for last_n, '2026-01-01' for since_date, '30' for within_days)",
     "desc_zh": "过滤参数（如 last_n 填 '10'，since_date 填 '2026-01-01'，within_days 填 '30'）"},

    {"key": "POST_VERIFY_MAX_RETRIES", "group": "post_verify", "type": "int", "default": "-1",
     "min": -1, "max": 100,
     "desc_en": "Max test-fix iterations (-1 = unlimited)",
     "desc_zh": "最大测试修复迭代次数（-1 = 无限制）"},

    {"key": "BASH_DEFAULT_TIMEOUT_MS", "group": "system", "type": "int", "default": "600000",
     "min": 10000, "max": 600000,
     "desc_en": "Default Bash command timeout in milliseconds",
     "desc_zh": "Bash 命令默认超时（毫秒）"},
    {"key": "BASH_MAX_TIMEOUT_MS", "group": "system", "type": "int", "default": "600000",
     "min": 10000, "max": 600000,
     "desc_en": "Maximum Bash command timeout in milliseconds",
     "desc_zh": "Bash 命令最大超时（毫秒）"},
    {"key": "REMY_LANG", "group": "system", "type": "enum", "default": "en",
     "options": ["en", "zh-CN"],
     "desc_en": "UI and output language",
     "desc_zh": "界面与输出语言"},
    {"key": "REPO_AUDIT_ROOT", "group": "system", "type": "text", "default": "~/claude_audit",
     "desc_en": "Root directory for repo-audit sandbox",
     "desc_zh": "仓库审计沙盒根目录"},

    {"key": "ANTHROPIC_API_KEY", "group": "claude_code", "type": "password", "default": "",
     "desc_en": "Anthropic API key for Claude Code",
     "desc_zh": "Claude Code 的 Anthropic API Key"},
    {"key": "ANTHROPIC_BASE_URL", "group": "claude_code", "type": "url", "default": "",
     "desc_en": "Custom Anthropic API base URL (leave empty for default)",
     "desc_zh": "自定义 Anthropic API 地址（留空使用默认值）"},
    {"key": "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "group": "claude_code", "type": "int", "default": "16384",
     "min": 1024, "max": 131072,
     "desc_en": "Max output tokens per Claude Code response",
     "desc_zh": "Claude Code 单次响应最大输出 token 数"},
]

GROUPS = [
    {"id": "llm_api", "label_en": "Logic Index", "label_zh": "语义索引"},
    {"id": "impact", "label_en": "Impact Analysis", "label_zh": "影响分析"},
    {"id": "injection", "label_en": "Context Injection", "label_zh": "上下文注入"},
    {"id": "timeline", "label_en": "Timeline", "label_zh": "时间线"},
    {"id": "post_verify", "label_en": "Post-Verify", "label_zh": "后验测试"},
    {"id": "system", "label_en": "System", "label_zh": "系统"},
    {"id": "claude_code", "label_en": "Claude Code", "label_zh": "Claude Code"},
]

LOCK_FILE = Path.home() / ".claude" / ".config_ui.lock"


def get_settings_path():
    return Path.home() / ".claude" / "settings.json"


def load_settings():
    path = get_settings_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json_file(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_pid_alive(pid):
    if pid <= 0:
        return False
    if sys.platform == "win32":
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


def acquire_lock(url, mode, target):
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
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
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "url": url, "mode": mode,
                    "target": str(target or "")}, f)


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


class ConfigHandler(http.server.BaseHTTPRequestHandler):
    html_path = None
    server_ref = None
    mode = "global"
    target_path = None

    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
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
                self._send_json({"error": "config_ui.html not found"}, 404)
        elif self.path == "/logo.svg":
            logo = Path(__file__).resolve().parent.parent / "remy-assets" / "logo.svg"
            if logo.exists():
                body = logo.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        elif self.path == "/api/config":
            try:
                global_settings = load_settings()
            except (json.JSONDecodeError, OSError) as e:
                self._send_json({"error": "Global settings read failed: " + str(e)}, 500)
                return
            env_global = global_settings.get("env", {})
            lang = env_global.get("REMY_LANG", "en")

            if self.mode == "project" and self.target_path:
                env_local = {}
                try:
                    env_local = load_json_file(self.target_path).get("env", {})
                except (json.JSONDecodeError, OSError):
                    pass
                self._send_json({
                    "mode": "project",
                    "target": str(self.target_path),
                    "env_global": env_global,
                    "env_local": env_local,
                    "lang": lang,
                    "registry": PARAM_REGISTRY,
                    "groups": GROUPS,
                })
            else:
                registered_keys = {p["key"] for p in PARAM_REGISTRY}
                unknown = [k for k in env_global if k not in registered_keys]
                if unknown:
                    print("  [i] Unregistered env keys: " + ", ".join(unknown))
                self._send_json({
                    "mode": "global",
                    "env": env_global,
                    "lang": lang,
                    "registry": PARAM_REGISTRY,
                    "groups": GROUPS,
                    "unknown_keys": unknown,
                })
        else:
            self.send_error(404)

    def _save_global(self, data):
        new_env = data.get("env", {})
        path = get_settings_path()
        if not path.exists():
            self._send_json({"status": "error",
                             "message": "settings.json not found. Run install.py first."}, 400)
            return
        shutil.copy2(path, path.with_suffix(".json.bak"))
        settings = load_settings()
        current_env = settings.get("env", {})
        current_env.update(new_env)
        settings["env"] = current_env
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if sys.platform != "win32":
            os.chmod(path, 0o600)
        self._send_json({"status": "ok"})

    def _save_project(self, data):
        new_env = data.get("env", {})
        overrides = set(data.get("overrides", []))
        path = self.target_path
        local_settings = {}
        if path.exists():
            shutil.copy2(path, path.with_suffix(".json.bak"))
            try:
                local_settings = load_json_file(path)
            except (json.JSONDecodeError, OSError):
                local_settings = {}
        local_env = {}
        for key in overrides:
            if key in new_env:
                local_env[key] = new_env[key]
        local_settings["env"] = local_env
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(local_settings, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if sys.platform != "win32":
            os.chmod(path, 0o600)
        self._send_json({"status": "ok"})

    def do_POST(self):
        if self.path == "/api/save":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                if self.mode == "project" and self.target_path:
                    self._save_project(data)
                else:
                    self._save_global(data)
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
        elif self.path == "/api/shutdown":
            try:
                self.send_response(200)
                self.end_headers()
            except Exception:
                pass
            if self.server_ref:
                threading.Thread(target=self.server_ref.shutdown, daemon=True).start()
        else:
            self.send_error(404)


def main(mode="global", target_path=None):
    html_path = Path(__file__).resolve().parent / "config_ui.html"
    ConfigHandler.html_path = html_path
    ConfigHandler.mode = mode

    if mode == "project" and target_path:
        project_dir = Path(target_path)
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        ConfigHandler.target_path = claude_dir / "settings.local.json"
    else:
        ConfigHandler.target_path = None

    server = http.server.HTTPServer(("127.0.0.1", 0), ConfigHandler)
    port = server.server_address[1]
    ConfigHandler.server_ref = server

    url = "http://127.0.0.1:{}".format(port)
    acquire_lock(url, mode, target_path)

    label = "Global" if mode == "global" else "Project"
    print("Remy Config UI ({}): {}".format(label, url))
    if mode == "project":
        print("  Target: " + str(ConfigHandler.target_path))
    print("Press Ctrl+C to stop.\n")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        release_lock()
        server.server_close()


if __name__ == "__main__":
    main()
