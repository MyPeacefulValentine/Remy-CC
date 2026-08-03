#!/usr/bin/env python3
"""Owned configuration storage and resolution for Remy-CC."""

from __future__ import annotations

import errno
import importlib
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

SCHEMA_VERSION = "1.0.0"
CONFIG_FILE_NAME = "remy-config.json"
CONFIG_LOCK_NAME = ".remy-config.lock"
CONFIG_LOCK_TIMEOUT = 5.0
SECRET_KEYS = frozenset({"REMY_LLM_API_KEY"})
INVALID_SECRET_VALUES = frozenset({"", "YOUR_API_KEY_HERE", "PROXY_MANAGED"})


class ConfigError(ValueError):
    """Raised when an explicit configuration operation finds invalid data."""


class ConfigLockTimeout(TimeoutError):
    """Raised when a configuration write lock cannot be acquired in time."""


@dataclass(frozen=True)
class FieldSpec:
    key: str
    old_keys: tuple[str, ...]
    value_type: str
    default: str
    group: str
    description_en: str
    description_zh: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    options: tuple[str, ...] = ()
    secret: bool = False
    project_allowed: bool = True
    ui_visible: bool = True
    allow_empty: bool = False
    path_base: Optional[str] = None


def _field(
    key: str,
    old_key: Optional[str],
    value_type: str,
    default: str,
    group: str,
    description_en: str,
    description_zh: str,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    options: Iterable[str] = (),
    secret: bool = False,
    project_allowed: bool = True,
    ui_visible: bool = True,
    allow_empty: bool = False,
    path_base: Optional[str] = None,
) -> FieldSpec:
    return FieldSpec(
        key=key,
        old_keys=(old_key,) if old_key else (),
        value_type=value_type,
        default=default,
        group=group,
        description_en=description_en,
        description_zh=description_zh,
        minimum=minimum,
        maximum=maximum,
        options=tuple(options),
        secret=secret,
        project_allowed=project_allowed,
        ui_visible=ui_visible,
        allow_empty=allow_empty,
        path_base=path_base,
    )


_FIELDS = (
    _field("REMY_LLM_API_KEY", "OPENAI_API_KEY", "password", "", "llm_api", "API key for the OpenAI-compatible LLM service", "OpenAI兼容LLM服务的API密钥", secret=True, project_allowed=False, allow_empty=True),
    _field("REMY_LLM_BASE_URL", "OPENAI_BASE_URL", "url", "https://api.deepseek.com/v1/chat/completions", "llm_api", "LLM API endpoint", "LLM API端点"),
    _field("REMY_LLM_MODEL", "OPENAI_MODEL", "text", "deepseek-v4-flash", "llm_api", "LLM model name", "LLM模型名称"),
    _field("REMY_LLM_MAX_WORKERS", "OPENAI_MAX_WORKERS", "int", "8", "llm_api", "Concurrent LLM request workers", "LLM并发请求线程数", minimum=1, maximum=64),
    _field("REMY_LLM_RETRY_LIMIT", "OPENAI_RETRY_LIMIT", "int", "8", "llm_api", "LLM request retry limit", "LLM请求重试次数", minimum=0, maximum=32),
    _field("REMY_LLM_TIMEOUT", "OPENAI_TIMEOUT", "int", "300", "llm_api", "LLM request timeout in seconds", "LLM请求超时秒数", minimum=30, maximum=3600),
    _field("REMY_LLM_MAX_TOKENS", "OPENAI_MAX_TOKENS", "int", "32768", "llm_api", "Maximum tokens in an LLM response", "LLM响应最大Token数", minimum=1024, maximum=1048576),
    _field("REMY_LOGIC_INDEX_FILTER_SMALL", "LOGIC_INDEX_FILTER_SMALL", "bool", "false", "llm_api", "Skip LLM summaries for small undocumented functions", "跳过无文档小函数的LLM摘要"),
    _field("REMY_LOGIC_INDEX_AUTO_INJECT", "LOGIC_INDEX_AUTO_INJECT", "enum", "ALWAYS", "injection", "Logic index injection policy", "逻辑索引注入策略", options=("ALWAYS", "ASK", "NEVER")),
    _field("REMY_LOGIC_INDEX_INTERACTIVE", "LOGIC_INDEX_INTERACTIVE", "bool", "true", "injection", "Show the logic scope selector at session start", "会话开始时显示逻辑范围选择器"),
    _field("REMY_LOGIC_SCOPE_TIMEOUT", "LOGIC_SCOPE_TIMEOUT", "int", "300", "injection", "Logic scope selector timeout in seconds", "逻辑范围选择器超时秒数", minimum=0, maximum=3600),
    _field("REMY_NAV_TIER_FULL_MAX", "NAV_TIER_FULL_MAX", "int", "200", "injection", "Maximum file count for full symbol injection", "完整符号注入的最大文件数", minimum=0, maximum=50000),
    _field("REMY_NAV_MCP_MINIMAL_ENABLED", "NAV_MCP_MINIMAL_ENABLED", "bool", "true", "injection", "Use minimal injection when MCP is available", "MCP可用时使用最小注入"),
    _field("REMY_NAV_TIER_CLUSTER_MAX", "NAV_TIER_CLUSTER_MAX", "int", "2000", "injection", "Maximum file count for cluster injection", "集群注入的最大文件数", minimum=0, maximum=100000),
    _field("REMY_LOGIC_INDEX_DB_PATH", "LOGIC_INDEX_DB_PATH", "path", ".claude/logic_index.db", "injection", "Logic index database path relative to the project root", "相对项目根的逻辑索引数据库路径", path_base="project"),
    _field("REMY_SCAN_COMMIT_BATCH_SIZE", "SCAN_COMMIT_BATCH_SIZE", "int", "100", "injection", "Files per full-scan transaction", "全量扫描每个事务的文件数", minimum=10, maximum=10000),
    _field("REMY_CLUSTER_DENSITY_THRESHOLD", "CLUSTER_DENSITY_THRESHOLD", "float", "0.5", "injection", "Minimum cluster edge density", "集群最小边密度", minimum=0.0),
    _field("REMY_CLUSTER_MAX_SIZE", "CLUSTER_MAX_SIZE", "int", "15", "injection", "Maximum files per cluster", "每个集群的最大文件数", minimum=2, maximum=200),
    _field("REMY_CLUSTER_ENTRY_COUNT", "CLUSTER_ENTRY_COUNT", "int", "3", "injection", "Entry symbols selected per cluster", "每个集群选择的入口符号数", minimum=1, maximum=20),
    _field("REMY_SYNTH_INTERFACE_FANOUT_CAP", "SYNTH_INTERFACE_FANOUT_CAP", "int", "10", "injection", "Interface dispatch synthetic edge cap", "接口分派合成边上限", minimum=1, maximum=100),
    _field("REMY_SYNTH_EVENT_FANOUT_CAP", "SYNTH_EVENT_FANOUT_CAP", "int", "20", "injection", "Event emitter synthetic edge cap", "事件发射器合成边上限", minimum=1, maximum=200),
    _field("REMY_RESOLVE_FANOUT_CAP", "RESOLVE_FANOUT_CAP", "int", "10", "injection", "Maximum ambiguous call resolution candidates", "歧义调用解析候选上限", minimum=1, maximum=100),
    _field("REMY_RESOLVE_SCORE_SAME_FILE", "RESOLVE_SCORE_SAME_FILE", "int", "2", "injection", "Same-file call resolution score", "同文件调用解析分数", minimum=0, maximum=100),
    _field("REMY_RESOLVE_SCORE_DIRECT_IMPORT", "RESOLVE_SCORE_DIRECT_IMPORT", "int", "1", "injection", "Direct-import call resolution score", "直接导入调用解析分数", minimum=0, maximum=100),
    _field("REMY_RESOLVE_SCORE_GLOBAL", "RESOLVE_SCORE_GLOBAL", "int", "0", "injection", "Global call resolution score", "全局调用解析分数", minimum=0, maximum=100),
    _field("REMY_ENRICHMENT_TIER_FULL_MAX", "ENRICHMENT_TIER_FULL_MAX", "int", "200", "impact", "Maximum file count for full enrichment", "完整富化的最大文件数", minimum=0, maximum=10000),
    _field("REMY_ENRICHMENT_TIER_MID_MAX", "ENRICHMENT_TIER_MID_MAX", "int", "1000", "impact", "Maximum file count for mid enrichment", "中等富化的最大文件数", minimum=0, maximum=50000),
    _field("REMY_ENRICHMENT_CAP", "ENRICHMENT_CAP", "int", "15", "impact", "Caller and callee cap for small and mid projects", "小中型项目的调用关系条目上限", minimum=1, maximum=100),
    _field("REMY_ENRICHMENT_CAP_LARGE", "ENRICHMENT_CAP_LARGE", "int", "10", "impact", "Caller and callee cap for large projects", "大型项目的调用关系条目上限", minimum=1, maximum=100),
    _field("REMY_ENRICHMENT_SIG_MAX_CHARS", "ENRICHMENT_SIG_MAX_CHARS", "int", "80", "impact", "Maximum signature characters in enrichment", "富化信息中的签名字符上限", minimum=0, maximum=500),
    _field("REMY_PROJECT_TREE_AUTO_INJECT", "PROJECT_TREE_AUTO_INJECT", "enum", "ALWAYS", "injection", "Project tree injection policy", "项目树注入策略", options=("ALWAYS", "ASK", "NEVER")),
    _field("REMY_TIMELINE_AUTO_INJECT", "TIMELINE_AUTO_INJECT", "enum", "ALWAYS", "injection", "Timeline injection policy", "时间线注入策略", options=("ALWAYS", "ASK", "NEVER")),
    _field("REMY_TIMELINE_INJECT_MODE", "TIMELINE_INJECT_MODE", "enum", "all", "timeline", "Timeline filter mode", "时间线过滤模式", options=("all", "last_n", "since_date", "within_days")),
    _field("REMY_TIMELINE_INJECT_VALUE", "TIMELINE_INJECT_VALUE", "text", "", "timeline", "Timeline filter value", "时间线过滤值", allow_empty=True),
    _field("REMY_MCP_SERVER_ENABLED", "MCP_SERVER_ENABLED", "bool", "true", "mcp", "Enable the remy-index MCP server on next launch", "下次启动时启用remy-index MCP服务器"),
    _field("REMY_MCP_BFS_MAX_DEPTH", "MCP_BFS_MAX_DEPTH", "int", "5", "mcp", "Maximum BFS query depth", "BFS查询最大深度", minimum=1, maximum=10),
    _field("REMY_MCP_RESULT_LIMIT", "MCP_RESULT_LIMIT", "int", "50", "mcp", "Shared MCP result limit", "MCP共享结果上限", minimum=10, maximum=500),
    _field("REMY_MCP_STATIC_ONLY_DEFAULT", "MCP_STATIC_ONLY_DEFAULT", "bool", "false", "mcp", "Default static-only graph query mode", "图查询默认仅使用静态边"),
    _field("REMY_FLOW_MAX_DEPTH", "FLOW_MAX_DEPTH", "int", "15", "mcp", "Maximum query_flow depth", "query_flow最大深度", minimum=1, maximum=50),
    _field("REMY_FLOW_MAX_VISITED", "FLOW_MAX_VISITED", "int", "2000", "mcp", "Maximum query_flow visited nodes", "query_flow最大访问节点数", minimum=100, maximum=50000),
    _field("REMY_SUMMARY_CHAR_LIMIT_SYMBOL", "SUMMARY_CHAR_LIMIT_SYMBOL", "int", "100", "summary", "Symbol summary character limit", "符号摘要字符上限", minimum=20, maximum=500),
    _field("REMY_SUMMARY_CHAR_LIMIT_FILE_COHESIVE", "SUMMARY_CHAR_LIMIT_FILE_COHESIVE", "int", "250", "summary", "Cohesive file summary character limit", "高内聚文件摘要字符上限", minimum=50, maximum=1000),
    _field("REMY_SUMMARY_CHAR_LIMIT_FILE_UTILITY", "SUMMARY_CHAR_LIMIT_FILE_UTILITY", "int", "800", "summary", "Utility file summary character limit", "工具文件摘要字符上限", minimum=100, maximum=4000),
    _field("REMY_SUMMARY_CHAR_LIMIT_CLUSTER", "SUMMARY_CHAR_LIMIT_CLUSTER", "int", "500", "summary", "Cluster summary character limit", "集群摘要字符上限", minimum=100, maximum=2000),
    _field("REMY_SUMMARY_ZH_LENGTH_FACTOR", "SUMMARY_ZH_LENGTH_FACTOR", "float", "0.5", "summary", "Chinese summary length multiplier", "中文摘要长度系数", minimum=0.1, maximum=1.0),
    _field("REMY_FILE_KIND_MIN_SYMBOLS", "FILE_KIND_MIN_SYMBOLS", "int", "5", "summary", "Minimum symbols for non-trivial file classification", "非简单文件分类所需的最小符号数", minimum=1, maximum=50),
    _field("REMY_FILE_KIND_LOW_COHESION_THRESHOLD", "FILE_KIND_LOW_COHESION_THRESHOLD", "float", "0.25", "summary", "Low-cohesion file threshold", "低内聚文件阈值", minimum=0.0, maximum=1.0),
    _field("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "int", "50", "summary", "Primary forced summary rewrite threshold", "摘要强制重写主阈值", minimum=1, maximum=10000),
    _field("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", "FORCE_RECOMPUTE_THRESHOLD_BACKUP", "int", "-1", "summary", "Backup forced summary rewrite threshold", "摘要强制重写备用阈值", minimum=-1, maximum=100000),
    _field("REMY_FORCE_RECOMPUTE_INTERVAL_DAYS", "FORCE_RECOMPUTE_INTERVAL_DAYS", "int", "30", "summary", "Forced summary rewrite interval in days", "摘要强制重写间隔天数", minimum=1, maximum=365),
    _field("REMY_SUMMARY_BOOTSTRAP_MODE", "SUMMARY_BOOTSTRAP_MODE", "enum", "auto", "summary", "Hierarchical summary bootstrap mode", "层级摘要初始化模式", options=("auto", "ask", "never")),
    _field("REMY_BOOTSTRAP_AUTO_SIZE_GUARD", "BOOTSTRAP_AUTO_SIZE_GUARD", "int", "500", "summary", "File-count guard for automatic bootstrap", "自动层级摘要的文件数限制", minimum=10, maximum=100000),
    _field("REMY_LANG", "REMY_LANG", "enum", "en", "system", "Remy output language", "Remy输出语言", options=("en", "zh-CN")),
    _field("REMY_BANNER_ENABLED", "REMY_BANNER_ENABLED", "bool", "true", "system", "Show the session-start banner", "显示会话启动横幅"),
    _field("REMY_REPO_AUDIT_ROOT", "REPO_AUDIT_ROOT", "path", "~/claude_audit", "system", "Repository audit sandbox root", "仓库审计沙盒根目录", path_base="user"),
    _field("REMY_STRUCT_SCAN_TIMEOUT", "STRUCT_SCAN_TIMEOUT", "int", "60", "system", "Lifecycle structural scan timeout in seconds", "生命周期结构扫描超时秒数", minimum=10, maximum=300),
    _field("REMY_INDEX_SCAN_LOCK_TIMEOUT", "INDEX_SCAN_LOCK_TIMEOUT", "float", "30", "system", "Project scan lock timeout in seconds", "项目扫描锁超时秒数", minimum=0, maximum=300),
    _field("REMY_INDEX_QUEUE_LOCK_TIMEOUT", "INDEX_QUEUE_LOCK_TIMEOUT", "float", "1", "system", "Dirty queue lock timeout in seconds", "脏路径队列锁超时秒数", minimum=0, maximum=30),
    _field("REMY_MIGRATION_KEEP_JSON", "MIGRATION_KEEP_JSON", "bool", "false", "system", "Keep the legacy JSON index after migration", "迁移后保留旧JSON索引", ui_visible=False),
    _field("REMY_EVAL_MODEL", "EVAL_MODEL", "text", "deepseek-v4-flash", "system", "Model used by the A/B evaluation agent", "A/B评估Agent使用的模型", ui_visible=False),
)

FIELD_SPECS: Mapping[str, FieldSpec] = MappingProxyType({field.key: field for field in _FIELDS})
OLD_TO_NEW: Mapping[str, str] = MappingProxyType({old: field.key for field in _FIELDS for old in field.old_keys})
UNUSED_OLD_KEYS = frozenset({
    "SUMMARY_LLM_TIMEOUT",
    "IMPACT_DEPTH_UP",
    "IMPACT_DEPTH_DOWN",
    "MCP_IMPACT_MAX_DEPTH_UP",
    "MCP_IMPACT_MAX_DEPTH_DOWN",
})
LEGACY_KEYS = frozenset(OLD_TO_NEW) | UNUSED_OLD_KEYS

GROUPS = (
    {"id": "llm_api", "label_en": "Logic Index LLM", "label_zh": "语义索引LLM"},
    {"id": "impact", "label_en": "Impact Analysis", "label_zh": "影响分析"},
    {"id": "injection", "label_en": "Context Injection", "label_zh": "上下文注入"},
    {"id": "timeline", "label_en": "Timeline", "label_zh": "时间线"},
    {"id": "mcp", "label_en": "MCP Server", "label_zh": "MCP服务器"},
    {"id": "summary", "label_en": "Summary Hierarchy", "label_zh": "层级摘要"},
    {"id": "system", "label_en": "System", "label_zh": "系统"},
)


@dataclass(frozen=True)
class ConfigSnapshot:
    values: Mapping[str, Any]
    raw_values: Mapping[str, str]
    sources: Mapping[str, str]
    diagnostics: tuple[str, ...]
    project_root: Optional[Path]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def get_int(self, key: str) -> int:
        value = self.values[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{key} is not an integer")
        return value

    def get_float(self, key: str) -> float:
        value = self.values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{key} is not a number")
        return float(value)

    def get_bool(self, key: str) -> bool:
        value = self.values[key]
        if not isinstance(value, bool):
            raise TypeError(f"{key} is not a boolean")
        return value

    def source_of(self, key: str) -> str:
        return self.sources.get(key, "unknown")

    def redacted_view(self) -> dict[str, Any]:
        return {
            key: ("<configured>" if key in SECRET_KEYS and bool(value) else value)
            for key, value in self.values.items()
        }


@dataclass(frozen=True)
class _FileData:
    values: Mapping[str, str]
    unknown: Mapping[str, str]
    diagnostics: tuple[str, ...]


_CACHE_LOCK = threading.RLock()
_FILE_CACHE: dict[str, tuple[Optional[tuple[int, int]], _FileData]] = {}
_WARNED_DIAGNOSTICS: set[str] = set()


def user_config_path() -> Path:
    return Path.home() / ".claude" / CONFIG_FILE_NAME


def project_config_path(project_root: Path | str) -> Path:
    return Path(project_root).resolve() / ".claude" / CONFIG_FILE_NAME


def discover_project_root(start: Optional[Path | str] = None) -> Optional[Path]:
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        claude_dir = candidate / ".claude"
        if any((claude_dir / name).exists() for name in (CONFIG_FILE_NAME, "logic_index_config", "logic_index.db")):
            return candidate
    return None


def _fingerprint(path: Path) -> Optional[tuple[int, int]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _parse_document(path: Path, *, strict: bool, project: bool) -> _FileData:
    if not path.exists():
        return _FileData(MappingProxyType({}), MappingProxyType({}), ())
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = f"Invalid Remy configuration file {path}: {type(exc).__name__}"
        if strict:
            raise ConfigError(message) from exc
        return _FileData(MappingProxyType({}), MappingProxyType({}), (message,))
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        message = f"Unsupported Remy configuration schema in {path}"
        if strict:
            raise ConfigError(message)
        return _FileData(MappingProxyType({}), MappingProxyType({}), (message,))
    raw_values = document.get("values")
    if not isinstance(raw_values, dict):
        message = f"Remy configuration values must be an object in {path}"
        if strict:
            raise ConfigError(message)
        return _FileData(MappingProxyType({}), MappingProxyType({}), (message,))
    values: dict[str, str] = {}
    unknown: dict[str, str] = {}
    diagnostics: list[str] = []
    for key, raw in raw_values.items():
        if not isinstance(key, str) or not isinstance(raw, str):
            message = f"Remy configuration field {key!r} must be a string in {path}"
            if strict:
                raise ConfigError(message)
            diagnostics.append(message)
            continue
        if key not in FIELD_SPECS:
            unknown[key] = raw
            continue
        if project and not FIELD_SPECS[key].project_allowed:
            message = f"Remy configuration field {key} is not allowed in project configuration"
            if strict:
                raise ConfigError(message)
            diagnostics.append(message)
            continue
        values[key] = raw
    return _FileData(MappingProxyType(values), MappingProxyType(unknown), tuple(diagnostics))


def _load_file(path: Path, *, strict: bool, project: bool) -> _FileData:
    if strict:
        return _parse_document(path, strict=True, project=project)
    cache_key = f"{path.resolve()}|{int(project)}"
    fingerprint = _fingerprint(path)
    with _CACHE_LOCK:
        cached = _FILE_CACHE.get(cache_key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        parsed = _parse_document(path, strict=False, project=project)
        _FILE_CACHE[cache_key] = (fingerprint, parsed)
        return parsed


def _coerce(spec: FieldSpec, raw: str) -> Any:
    if not isinstance(raw, str):
        raise ConfigError(f"{spec.key} must be a string")
    if raw == "" and not spec.allow_empty:
        raise ConfigError(f"{spec.key} must not be empty")
    try:
        if spec.value_type == "int":
            value: Any = int(raw)
        elif spec.value_type == "float":
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("non-finite number")
        elif spec.value_type == "bool":
            normalized = raw.lower()
            if normalized not in ("true", "false"):
                raise ValueError("expected true or false")
            value = normalized == "true"
        else:
            value = raw
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{spec.key} has invalid {spec.value_type} syntax") from exc
    if spec.options and raw not in spec.options:
        raise ConfigError(f"{spec.key} must be one of {', '.join(spec.options)}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if spec.minimum is not None and value < spec.minimum:
            raise ConfigError(f"{spec.key} must be >= {spec.minimum:g}")
        if spec.maximum is not None and value > spec.maximum:
            raise ConfigError(f"{spec.key} must be <= {spec.maximum:g}")
    return value


def _resolve_path(spec: FieldSpec, value: Any, project_root: Optional[Path]) -> Any:
    if spec.value_type != "path" or not isinstance(value, str):
        return value
    expanded = Path(os.path.expanduser(value))
    if expanded.is_absolute():
        return str(expanded.resolve())
    if spec.path_base == "project" and project_root is not None:
        return str((project_root / expanded).resolve())
    if spec.path_base == "user":
        return str((Path.home() / expanded).resolve())
    return value


def load_config(project_root: Optional[Path | str] = None, strict: bool = False) -> ConfigSnapshot:
    root = Path(project_root).resolve() if project_root is not None else discover_project_root()
    user_data = _load_file(user_config_path(), strict=strict, project=False)
    project_data = (
        _load_file(project_config_path(root), strict=strict, project=True)
        if root is not None
        else _FileData(MappingProxyType({}), MappingProxyType({}), ())
    )
    diagnostics = list(user_data.diagnostics) + list(project_data.diagnostics)
    values: dict[str, Any] = {}
    raw_values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key, spec in FIELD_SPECS.items():
        candidates: list[tuple[str, str]] = []
        if key in os.environ:
            candidates.append(("environment", os.environ[key]))
        if key in project_data.values:
            candidates.append(("project", project_data.values[key]))
        if key in user_data.values:
            candidates.append(("user", user_data.values[key]))
        candidates.append(("default", spec.default))
        for source, raw in candidates:
            try:
                value = _coerce(spec, raw)
            except ConfigError as exc:
                message = f"{exc} (source={source})"
                if strict:
                    raise ConfigError(message) from exc
                diagnostics.append(message)
                continue
            values[key] = _resolve_path(spec, value, root)
            raw_values[key] = raw
            sources[key] = source
            break
    return ConfigSnapshot(
        values=MappingProxyType(values),
        raw_values=MappingProxyType(raw_values),
        sources=MappingProxyType(sources),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
        project_root=root,
    )


def emit_diagnostics(snapshot: ConfigSnapshot, *, prefix: str = "RemyConfig") -> None:
    for diagnostic in snapshot.diagnostics:
        with _CACHE_LOCK:
            if diagnostic in _WARNED_DIAGNOSTICS:
                continue
            _WARNED_DIAGNOSTICS.add(diagnostic)
        print(f"[{prefix}] {diagnostic}", file=sys.stderr)


def registry_for_ui() -> list[dict[str, Any]]:
    result = []
    for spec in _FIELDS:
        if not spec.ui_visible:
            continue
        row: dict[str, Any] = {
            "key": spec.key,
            "group": spec.group,
            "type": spec.value_type,
            "default": spec.default,
            "desc_en": spec.description_en,
            "desc_zh": spec.description_zh,
            "secret": spec.secret,
            "project_allowed": spec.project_allowed,
        }
        if spec.minimum is not None:
            row["min"] = spec.minimum
        if spec.maximum is not None:
            row["max"] = spec.maximum
        if spec.options:
            row["options"] = list(spec.options)
        result.append(row)
    return result


def inspect_document(
    path: Path | str,
    *,
    project: bool = False,
) -> dict[str, Any]:
    target = Path(path)
    parsed = _parse_document(target, strict=False, project=project)
    values = dict(parsed.unknown)
    values.update(parsed.values)
    return {
        "exists": target.exists(),
        "valid": not parsed.diagnostics,
        "values": values,
        "diagnostics": parsed.diagnostics,
    }


def read_document(path: Path | str, *, strict: bool = True, project: bool = False) -> dict[str, Any]:
    target = Path(path)
    parsed = _parse_document(target, strict=strict, project=project)
    values = dict(parsed.unknown)
    values.update(parsed.values)
    return {"schema_version": SCHEMA_VERSION, "values": values}


def validate_document(path: Path | str, *, project: bool = False) -> dict[str, Any]:
    document = read_document(path, strict=True, project=project)
    for key, raw in document["values"].items():
        spec = FIELD_SPECS.get(key)
        if spec is None:
            continue
        _coerce(spec, raw)
    return document


def _lock_byte(handle: Any, nonblocking: bool) -> None:
    try:
        msvcrt = importlib.import_module("msvcrt")
    except ImportError:
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK, 1)


def _unlock_byte(handle: Any) -> None:
    try:
        msvcrt = importlib.import_module("msvcrt")
    except ImportError:
        importlib.import_module("fcntl").flock(handle.fileno(), importlib.import_module("fcntl").LOCK_UN)
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class ConfigFileLock:
    def __init__(self, directory: Path, timeout: float = CONFIG_LOCK_TIMEOUT):
        self.path = directory / CONFIG_LOCK_NAME
        self.timeout = max(0.0, timeout)
        self._handle: Any = None

    def acquire(self) -> "ConfigFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                _lock_byte(handle, nonblocking=True)
                self._handle = handle
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13):
                    handle.close()
                    raise
                if time.monotonic() >= deadline:
                    handle.close()
                    raise ConfigLockTimeout(f"Timed out acquiring Remy config lock: {self.path}") from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            _unlock_byte(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "ConfigFileLock":
        return self.acquire()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if sys.platform != "win32":
            os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        if sys.platform != "win32":
            os.chmod(path, 0o600)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def save_config(
    path: Path | str,
    updates: Optional[Mapping[str, str]] = None,
    *,
    remove_keys: Iterable[str] = (),
    clear_secrets: Iterable[str] = (),
    project: bool = False,
    timeout: float = CONFIG_LOCK_TIMEOUT,
) -> dict[str, Any]:
    target = Path(path)
    with ConfigFileLock(target.parent, timeout=timeout):
        document = read_document(target, strict=True, project=project) if target.exists() else {"schema_version": SCHEMA_VERSION, "values": {}}
        values = dict(document["values"])
        for key in remove_keys:
            if key in FIELD_SPECS:
                values.pop(key, None)
        for key in clear_secrets:
            if key not in SECRET_KEYS:
                raise ConfigError(f"{key} is not a secret field")
            values.pop(key, None)
        for key, raw in (updates or {}).items():
            spec = FIELD_SPECS.get(key)
            if spec is None:
                raise ConfigError(f"Unknown Remy configuration field {key}")
            if project and not spec.project_allowed:
                raise ConfigError(f"{key} is not allowed in project configuration")
            _coerce(spec, raw)
            values[key] = raw
        output = {"schema_version": SCHEMA_VERSION, "values": values}
        _atomic_write_json(target, output)
        read_document(target, strict=True, project=project)
        with _CACHE_LOCK:
            for cache_key in tuple(_FILE_CACHE):
                if cache_key.startswith(str(target.resolve()) + "|"):
                    _FILE_CACHE.pop(cache_key, None)
        return output


def reset_non_secret_values(path: Path | str, *, project: bool = False) -> dict[str, Any]:
    return save_config(
        path,
        remove_keys=(key for key in FIELD_SPECS if key not in SECRET_KEYS),
        project=project,
    )


def reset_known_values(path: Path | str, *, project: bool = False) -> dict[str, Any]:
    return save_config(path, remove_keys=FIELD_SPECS.keys(), project=project)


def legacy_values_from_settings(settings: Mapping[str, Any], *, project: bool = False) -> dict[str, str]:
    env = settings.get("env", {}) if isinstance(settings, Mapping) else {}
    if not isinstance(env, Mapping):
        return {}
    migrated: dict[str, str] = {}
    for old_key, new_key in OLD_TO_NEW.items():
        if old_key not in env or new_key in migrated:
            continue
        spec = FIELD_SPECS[new_key]
        if project and not spec.project_allowed:
            continue
        raw = env[old_key]
        if not isinstance(raw, str):
            continue
        if spec.secret and raw.strip() in INVALID_SECRET_VALUES:
            continue
        try:
            _coerce(spec, raw)
        except ConfigError:
            continue
        migrated[new_key] = raw
    return migrated


def migrate_settings_file(
    settings_path: Path | str,
    target_path: Path | str,
    *,
    project: bool = False,
) -> dict[str, Any]:
    settings_target = Path(settings_path)
    config_target = Path(target_path)
    if not settings_target.exists():
        return {"migrated": (), "removed": (), "backup": None}
    try:
        settings = json.loads(settings_target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Invalid Claude settings file {settings_target}: {type(exc).__name__}") from exc
    if not isinstance(settings, dict):
        raise ConfigError(f"Claude settings file must contain an object: {settings_target}")
    migrated = legacy_values_from_settings(settings, project=project)
    existing = read_document(config_target, strict=True, project=project) if config_target.exists() else {"schema_version": SCHEMA_VERSION, "values": {}}
    missing = {key: value for key, value in migrated.items() if key not in existing["values"]}
    if missing or not config_target.exists():
        save_config(config_target, missing, project=project)
    read_document(config_target, strict=True, project=project)
    env = settings.get("env")
    removed: list[str] = []
    if isinstance(env, dict):
        for key in sorted(LEGACY_KEYS):
            if key in env:
                env.pop(key)
                removed.append(key)
        if not env:
            settings.pop("env", None)
    backup_path = settings_target.with_name(settings_target.name + ".remy-pre-migration.bak")
    if removed:
        if not backup_path.exists():
            backup_path.write_bytes(settings_target.read_bytes())
            if sys.platform != "win32":
                os.chmod(backup_path, 0o600)
        _atomic_write_json(settings_target, settings)
    return {"migrated": tuple(sorted(missing)), "removed": tuple(removed), "backup": backup_path if removed else None}
