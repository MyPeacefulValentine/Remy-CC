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
RESTART_SCOPES = ("immediate", "next_index", "next_session", "next_mcp_launch")


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
    label_en: str = ""
    label_zh: str = ""
    unit_en: Optional[str] = None
    unit_zh: Optional[str] = None
    advanced: bool = True
    restart_scope: str = "immediate"


def _field(
    key: str,
    old_key: Optional[str],
    value_type: str,
    default: str,
    group: str,
    description_en: str,
    description_zh: str,
    *,
    label_en: str,
    label_zh: str,
    restart_scope: str,
    unit_en: Optional[str] = None,
    unit_zh: Optional[str] = None,
    advanced: bool = True,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    options: Iterable[str] = (),
    secret: bool = False,
    project_allowed: bool = True,
    ui_visible: bool = True,
    allow_empty: bool = False,
    path_base: Optional[str] = None,
) -> FieldSpec:
    if restart_scope not in RESTART_SCOPES:
        raise ValueError(f"{key} restart_scope must be one of {', '.join(RESTART_SCOPES)}")
    if (unit_en is None) != (unit_zh is None):
        raise ValueError(f"{key} must declare unit_en and unit_zh together")
    if not label_en or not label_zh:
        raise ValueError(f"{key} must declare bilingual labels")
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
        label_en=label_en,
        label_zh=label_zh,
        unit_en=unit_en,
        unit_zh=unit_zh,
        advanced=advanced,
        restart_scope=restart_scope,
    )


_FIELDS = (
    _field("REMY_LLM_API_KEY", "OPENAI_API_KEY", "password", "", "llm_api", "API key for the OpenAI-compatible LLM service", "OpenAI兼容LLM服务的API密钥", label_en="API Key", label_zh="API密钥", restart_scope="next_index", advanced=False, secret=True, project_allowed=False, allow_empty=True),
    _field("REMY_LLM_BASE_URL", "OPENAI_BASE_URL", "url", "https://api.deepseek.com/v1/chat/completions", "llm_api", "LLM API endpoint", "LLM API端点", label_en="API Endpoint", label_zh="API端点", restart_scope="next_index", advanced=False),
    _field("REMY_LLM_MODEL", "OPENAI_MODEL", "text", "deepseek-v4-flash", "llm_api", "LLM model name", "LLM模型名称", label_en="Model Name", label_zh="模型名称", restart_scope="next_index", advanced=False),
    _field("REMY_LLM_MAX_WORKERS", "OPENAI_MAX_WORKERS", "int", "8", "llm_api", "Concurrent LLM request workers", "LLM并发请求线程数", label_en="Concurrent Requests", label_zh="并发请求数", unit_en="requests", unit_zh="请求", restart_scope="next_index", advanced=False, minimum=1, maximum=64),
    _field("REMY_LLM_RETRY_LIMIT", "OPENAI_RETRY_LIMIT", "int", "8", "llm_api", "LLM request retry limit", "LLM请求重试次数", label_en="Retry Limit", label_zh="重试次数上限", unit_en="retries", unit_zh="次", restart_scope="next_index", minimum=0, maximum=32),
    _field("REMY_LLM_TIMEOUT", "OPENAI_TIMEOUT", "int", "300", "llm_api", "LLM request timeout in seconds", "LLM请求超时秒数", label_en="Request Timeout", label_zh="请求超时", unit_en="seconds", unit_zh="秒", restart_scope="next_index", minimum=30, maximum=3600),
    _field("REMY_LLM_MAX_TOKENS", "OPENAI_MAX_TOKENS", "int", "32768", "llm_api", "Maximum tokens in an LLM response", "LLM响应最大Token数", label_en="Response Token Limit", label_zh="响应Token上限", unit_en="tokens", unit_zh="Token", restart_scope="next_index", minimum=1024, maximum=1048576),
    _field("REMY_LOGIC_INDEX_FILTER_SMALL", "LOGIC_INDEX_FILTER_SMALL", "bool", "false", "index_generation", "Skip LLM summaries for small undocumented functions", "跳过无文档小函数的LLM摘要", label_en="Small Function Filter", label_zh="小函数摘要过滤", restart_scope="next_index"),
    _field("REMY_LOGIC_INDEX_DB_PATH", "LOGIC_INDEX_DB_PATH", "path", ".claude/logic_index.db", "index_generation", "Logic index database path relative to the project root", "相对项目根的逻辑索引数据库路径", label_en="Index Database Path", label_zh="索引数据库路径", restart_scope="next_index", path_base="project"),
    _field("REMY_SCAN_COMMIT_BATCH_SIZE", "SCAN_COMMIT_BATCH_SIZE", "int", "100", "index_generation", "Files per full-scan transaction", "全量扫描每个事务的文件数", label_en="Scan Commit Batch", label_zh="扫描提交批量", unit_en="files", unit_zh="文件", restart_scope="next_index", minimum=10, maximum=10000),
    _field("REMY_CLUSTER_DENSITY_THRESHOLD", "CLUSTER_DENSITY_THRESHOLD", "float", "0.5", "index_generation", "Minimum cluster edge density", "集群最小边密度", label_en="Cluster Density Threshold", label_zh="集群密度阈值", restart_scope="next_index", minimum=0.0),
    _field("REMY_CLUSTER_MAX_SIZE", "CLUSTER_MAX_SIZE", "int", "15", "index_generation", "Maximum files per cluster", "每个集群的最大文件数", label_en="Cluster Size Limit", label_zh="集群大小上限", unit_en="files", unit_zh="文件", restart_scope="next_index", minimum=2, maximum=200),
    _field("REMY_CLUSTER_ENTRY_COUNT", "CLUSTER_ENTRY_COUNT", "int", "3", "index_generation", "Entry symbols selected per cluster", "每个集群选择的入口符号数", label_en="Cluster Entry Symbols", label_zh="集群入口符号数", unit_en="symbols", unit_zh="符号", restart_scope="next_index", minimum=1, maximum=20),
    _field("REMY_SYNTH_INTERFACE_FANOUT_CAP", "SYNTH_INTERFACE_FANOUT_CAP", "int", "10", "index_generation", "Interface dispatch synthetic edge cap", "接口分派合成边上限", label_en="Interface Edge Cap", label_zh="接口合成边上限", unit_en="edges", unit_zh="边", restart_scope="next_index", minimum=1, maximum=100),
    _field("REMY_SYNTH_EVENT_FANOUT_CAP", "SYNTH_EVENT_FANOUT_CAP", "int", "20", "index_generation", "Event emitter synthetic edge cap", "事件发射器合成边上限", label_en="Event Edge Cap", label_zh="事件合成边上限", unit_en="edges", unit_zh="边", restart_scope="next_index", minimum=1, maximum=200),
    _field("REMY_RESOLVE_FANOUT_CAP", "RESOLVE_FANOUT_CAP", "int", "10", "index_generation", "Maximum ambiguous call resolution candidates", "歧义调用解析候选上限", label_en="Resolution Candidate Cap", label_zh="调用解析候选上限", unit_en="candidates", unit_zh="候选", restart_scope="next_index", minimum=1, maximum=100),
    _field("REMY_RESOLVE_SCORE_SAME_FILE", "RESOLVE_SCORE_SAME_FILE", "int", "2", "index_generation", "Same-file call resolution score", "同文件调用解析分数", label_en="Same-File Resolution Score", label_zh="同文件解析分数", restart_scope="next_index", minimum=0, maximum=100),
    _field("REMY_RESOLVE_SCORE_DIRECT_IMPORT", "RESOLVE_SCORE_DIRECT_IMPORT", "int", "1", "index_generation", "Direct-import call resolution score", "直接导入调用解析分数", label_en="Direct-Import Resolution Score", label_zh="直接导入解析分数", restart_scope="next_index", minimum=0, maximum=100),
    _field("REMY_RESOLVE_SCORE_GLOBAL", "RESOLVE_SCORE_GLOBAL", "int", "0", "index_generation", "Global call resolution score", "全局调用解析分数", label_en="Global Resolution Score", label_zh="全局解析分数", restart_scope="next_index", minimum=0, maximum=100),
    _field("REMY_LOGIC_INDEX_AUTO_INJECT", "LOGIC_INDEX_AUTO_INJECT", "enum", "ALWAYS", "injection", "Logic index injection policy", "逻辑索引注入策略", label_en="Index Injection Policy", label_zh="索引注入策略", restart_scope="next_session", advanced=False, options=("ALWAYS", "ASK", "NEVER")),
    _field("REMY_PROJECT_TREE_AUTO_INJECT", "PROJECT_TREE_AUTO_INJECT", "enum", "ALWAYS", "injection", "Project tree injection policy", "项目树注入策略", label_en="Project Tree Injection Policy", label_zh="项目树注入策略", restart_scope="next_session", options=("ALWAYS", "ASK", "NEVER")),
    _field("REMY_TIMELINE_AUTO_INJECT", "TIMELINE_AUTO_INJECT", "enum", "ALWAYS", "injection", "Timeline injection policy", "时间线注入策略", label_en="Timeline Injection Policy", label_zh="时间线注入策略", restart_scope="next_session", options=("ALWAYS", "ASK", "NEVER")),
    _field("REMY_ENRICHMENT_TIER_FULL_MAX", "ENRICHMENT_TIER_FULL_MAX", "int", "200", "injection", "Maximum file count for full enrichment", "完整富化的最大文件数", label_en="Full Enrichment File Cap", label_zh="完整富化文件上限", unit_en="files", unit_zh="文件", restart_scope="immediate", minimum=0, maximum=10000),
    _field("REMY_ENRICHMENT_TIER_MID_MAX", "ENRICHMENT_TIER_MID_MAX", "int", "1000", "injection", "Maximum file count for mid enrichment", "中等富化的最大文件数", label_en="Mid Enrichment File Cap", label_zh="中等富化文件上限", unit_en="files", unit_zh="文件", restart_scope="immediate", minimum=0, maximum=50000),
    _field("REMY_ENRICHMENT_CAP", "ENRICHMENT_CAP", "int", "15", "injection", "Caller and callee cap for small and mid projects", "小中型项目的调用关系条目上限", label_en="Enrichment Entry Cap", label_zh="富化条目上限", unit_en="entries", unit_zh="条", restart_scope="immediate", minimum=1, maximum=100),
    _field("REMY_ENRICHMENT_CAP_LARGE", "ENRICHMENT_CAP_LARGE", "int", "10", "injection", "Caller and callee cap for large projects", "大型项目的调用关系条目上限", label_en="Large-Project Enrichment Cap", label_zh="大型项目富化上限", unit_en="entries", unit_zh="条", restart_scope="immediate", minimum=1, maximum=100),
    _field("REMY_ENRICHMENT_SIG_MAX_CHARS", "ENRICHMENT_SIG_MAX_CHARS", "int", "80", "injection", "Maximum signature characters in enrichment", "富化信息中的签名字符上限", label_en="Enrichment Signature Length", label_zh="富化签名长度上限", unit_en="characters", unit_zh="字符", restart_scope="immediate", minimum=0, maximum=500),
    _field("REMY_MCP_SERVER_ENABLED", "MCP_SERVER_ENABLED", "bool", "true", "mcp", "Enable the remy-index MCP server on next launch", "下次启动时启用remy-index MCP服务器", label_en="MCP Server Switch", label_zh="MCP服务器开关", restart_scope="next_mcp_launch", advanced=False),
    _field("REMY_MCP_BFS_MAX_DEPTH", "MCP_BFS_MAX_DEPTH", "int", "5", "mcp", "Maximum BFS query depth", "BFS查询最大深度", label_en="BFS Depth Limit", label_zh="BFS深度上限", unit_en="levels", unit_zh="层", restart_scope="immediate", minimum=1, maximum=10),
    _field("REMY_MCP_RESULT_LIMIT", "MCP_RESULT_LIMIT", "int", "50", "mcp", "Shared MCP result limit", "MCP共享结果上限", label_en="Result Limit", label_zh="结果条数上限", unit_en="entries", unit_zh="条", restart_scope="immediate", minimum=10, maximum=500),
    _field("REMY_MCP_STATIC_ONLY_DEFAULT", "MCP_STATIC_ONLY_DEFAULT", "bool", "false", "mcp", "Default static-only graph query mode", "图查询默认仅使用静态边", label_en="Static-Only Default", label_zh="默认仅静态边", restart_scope="immediate"),
    _field("REMY_FLOW_MAX_DEPTH", "FLOW_MAX_DEPTH", "int", "15", "mcp", "Maximum query_flow depth", "query_flow最大深度", label_en="Flow Depth Limit", label_zh="调用路径深度上限", unit_en="levels", unit_zh="层", restart_scope="immediate", minimum=1, maximum=50),
    _field("REMY_FLOW_MAX_VISITED", "FLOW_MAX_VISITED", "int", "2000", "mcp", "Maximum query_flow visited nodes", "query_flow最大访问节点数", label_en="Flow Node Limit", label_zh="调用路径节点上限", unit_en="nodes", unit_zh="节点", restart_scope="immediate", minimum=100, maximum=50000),
    _field("REMY_NAVIGATE_CANDIDATE_CLUSTERS", None, "int", "5", "mcp", "Maximum cluster candidates per query_navigate intent", "query_navigate每次意图查询的cluster候选上限", label_en="Navigate Cluster Candidates", label_zh="导航cluster候选上限", unit_en="entries", unit_zh="条", restart_scope="immediate", minimum=1, maximum=50),
    _field("REMY_NAVIGATE_CANDIDATE_FILES", None, "int", "10", "mcp", "Maximum file candidates per query_navigate intent", "query_navigate每次意图查询的file候选上限", label_en="Navigate File Candidates", label_zh="导航file候选上限", unit_en="entries", unit_zh="条", restart_scope="immediate", minimum=1, maximum=50),
    _field("REMY_NAVIGATE_CANDIDATE_SYMBOLS", None, "int", "10", "mcp", "Maximum symbol candidates per query_navigate intent", "query_navigate每次意图查询的symbol候选上限", label_en="Navigate Symbol Candidates", label_zh="导航symbol候选上限", unit_en="entries", unit_zh="条", restart_scope="immediate", minimum=1, maximum=50),
    _field("REMY_SUMMARY_CHAR_LIMIT_SYMBOL", "SUMMARY_CHAR_LIMIT_SYMBOL", "int", "100", "summary", "Symbol summary character limit", "符号摘要字符上限", label_en="Symbol Summary Length", label_zh="符号摘要长度", unit_en="characters", unit_zh="字符", restart_scope="next_index", minimum=20, maximum=500),
    _field("REMY_SUMMARY_CHAR_LIMIT_FILE_COHESIVE", "SUMMARY_CHAR_LIMIT_FILE_COHESIVE", "int", "250", "summary", "Cohesive file summary character limit", "高内聚文件摘要字符上限", label_en="Cohesive File Summary Length", label_zh="高内聚文件摘要长度", unit_en="characters", unit_zh="字符", restart_scope="next_index", minimum=50, maximum=1000),
    _field("REMY_SUMMARY_CHAR_LIMIT_FILE_UTILITY", "SUMMARY_CHAR_LIMIT_FILE_UTILITY", "int", "800", "summary", "Utility file summary character limit", "工具文件摘要字符上限", label_en="Utility File Summary Length", label_zh="工具文件摘要长度", unit_en="characters", unit_zh="字符", restart_scope="next_index", minimum=100, maximum=4000),
    _field("REMY_SUMMARY_CHAR_LIMIT_CLUSTER", "SUMMARY_CHAR_LIMIT_CLUSTER", "int", "500", "summary", "Cluster summary character limit", "集群摘要字符上限", label_en="Cluster Summary Length", label_zh="集群摘要长度", unit_en="characters", unit_zh="字符", restart_scope="next_index", minimum=100, maximum=2000),
    _field("REMY_FILE_KIND_MIN_SYMBOLS", "FILE_KIND_MIN_SYMBOLS", "int", "5", "summary", "Minimum symbols for non-trivial file classification", "非简单文件分类所需的最小符号数", label_en="File Classification Symbol Floor", label_zh="文件分类符号下限", unit_en="symbols", unit_zh="符号", restart_scope="next_index", minimum=1, maximum=50),
    _field("REMY_FILE_KIND_LOW_COHESION_THRESHOLD", "FILE_KIND_LOW_COHESION_THRESHOLD", "float", "0.25", "summary", "Low-cohesion file threshold", "低内聚文件阈值", label_en="Low-Cohesion Threshold", label_zh="低内聚阈值", restart_scope="next_index", minimum=0.0, maximum=1.0),
    _field("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "int", "50", "summary", "Primary forced summary rewrite threshold", "摘要强制重写主阈值", label_en="Forced Rewrite Threshold", label_zh="强制重写主阈值", unit_en="changes", unit_zh="次", restart_scope="next_index", minimum=1, maximum=10000),
    _field("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", "FORCE_RECOMPUTE_THRESHOLD_BACKUP", "int", "-1", "summary", "Backup forced summary rewrite threshold", "摘要强制重写备用阈值", label_en="Backup Rewrite Threshold", label_zh="强制重写备用阈值", unit_en="changes", unit_zh="次", restart_scope="next_index", minimum=-1, maximum=100000),
    _field("REMY_FORCE_RECOMPUTE_INTERVAL_DAYS", "FORCE_RECOMPUTE_INTERVAL_DAYS", "int", "30", "summary", "Forced summary rewrite interval in days", "摘要强制重写间隔天数", label_en="Forced Rewrite Interval", label_zh="强制重写间隔", unit_en="days", unit_zh="天", restart_scope="next_index", minimum=1, maximum=365),
    _field("REMY_SUMMARY_BOOTSTRAP_MODE", "SUMMARY_BOOTSTRAP_MODE", "enum", "auto", "summary", "Hierarchical summary bootstrap mode", "层级摘要初始化模式", label_en="Summary Bootstrap Mode", label_zh="摘要初始化模式", restart_scope="next_index", options=("auto", "ask", "never")),
    _field("REMY_BOOTSTRAP_AUTO_SIZE_GUARD", "BOOTSTRAP_AUTO_SIZE_GUARD", "int", "500", "summary", "File-count guard for automatic bootstrap", "自动层级摘要的文件数限制", label_en="Auto Bootstrap File Guard", label_zh="自动初始化文件上限", unit_en="files", unit_zh="文件", restart_scope="next_index", minimum=10, maximum=100000),
    _field("REMY_TIMELINE_INJECT_MODE", "TIMELINE_INJECT_MODE", "enum", "all", "timeline", "Timeline filter mode", "时间线过滤模式", label_en="Timeline Filter Mode", label_zh="时间线过滤模式", restart_scope="next_session", options=("all", "last_n", "since_date", "within_days")),
    _field("REMY_TIMELINE_INJECT_VALUE", "TIMELINE_INJECT_VALUE", "text", "", "timeline", "Timeline filter value", "时间线过滤值", label_en="Timeline Filter Value", label_zh="时间线过滤值", restart_scope="next_session", allow_empty=True),
    _field("REMY_LANG", "REMY_LANG", "enum", "en", "system", "Remy output language", "Remy输出语言", label_en="Interface Language", label_zh="输出语言", restart_scope="next_session", advanced=False, options=("en", "zh-CN")),
    _field("REMY_BANNER_ENABLED", "REMY_BANNER_ENABLED", "bool", "true", "system", "Show the session-start banner", "显示会话启动横幅", label_en="Startup Banner", label_zh="启动横幅", restart_scope="next_session"),
    _field("REMY_PERMISSION_GATE", None, "bool", "true", "system", "Auto-approve Edit/Write permission prompts for project .claude/ system artifacts", "自动放行项目级.claude/系统工件的Edit/Write权限弹窗", label_en="Permission Gate", label_zh="权限闸门", restart_scope="immediate", advanced=False),
    _field("REMY_REPO_AUDIT_ROOT", "REPO_AUDIT_ROOT", "path", "~/claude_audit", "system", "Repository audit sandbox root", "仓库审计沙盒根目录", label_en="Audit Sandbox Root", label_zh="审计沙盒根目录", restart_scope="immediate", path_base="user"),
    _field("REMY_STRUCT_SCAN_TIMEOUT", "STRUCT_SCAN_TIMEOUT", "int", "60", "system", "Lifecycle structural scan timeout in seconds", "生命周期结构扫描超时秒数", label_en="Structural Scan Timeout", label_zh="结构扫描超时", unit_en="seconds", unit_zh="秒", restart_scope="next_session", minimum=10, maximum=300),
    _field("REMY_FULL_SCAN_TIMEOUT", None, "int", "1800", "system", "Daemon full-scan job timeout in seconds", "daemon全量扫描作业超时秒数", label_en="Full Scan Timeout", label_zh="全量扫描超时", unit_en="seconds", unit_zh="秒", restart_scope="next_session", minimum=60, maximum=86400),
    _field("REMY_SCANNER_PROVIDER", None, "enum", "python", "system", "Scanner provider the daemon publishes after validation", "daemon验证后发布的扫描器provider", label_en="Scanner Provider", label_zh="扫描器Provider", restart_scope="next_session", options=("python", "rust")),
    _field("REMY_INDEX_SCAN_LOCK_TIMEOUT", "INDEX_SCAN_LOCK_TIMEOUT", "float", "30", "system", "Project scan lock timeout in seconds", "项目扫描锁超时秒数", label_en="Scan Lock Timeout", label_zh="扫描锁超时", unit_en="seconds", unit_zh="秒", restart_scope="next_session", minimum=0, maximum=300),
    _field("REMY_INDEX_QUEUE_LOCK_TIMEOUT", "INDEX_QUEUE_LOCK_TIMEOUT", "float", "1", "system", "Dirty queue lock timeout in seconds", "脏路径队列锁超时秒数", label_en="Queue Lock Timeout", label_zh="队列锁超时", unit_en="seconds", unit_zh="秒", restart_scope="immediate", minimum=0, maximum=30),
    _field("REMY_MIGRATION_KEEP_JSON", "MIGRATION_KEEP_JSON", "bool", "false", "system", "Keep the legacy JSON index after migration", "迁移后保留旧JSON索引", label_en="Legacy JSON Retention", label_zh="旧JSON索引保留", restart_scope="immediate", ui_visible=False),
    _field("REMY_EVAL_MODEL", "EVAL_MODEL", "text", "deepseek-v4-flash", "system", "Model used by the A/B evaluation agent", "A/B评估Agent使用的模型", label_en="Evaluation Model", label_zh="评估模型", restart_scope="immediate", ui_visible=False),
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
    {"id": "llm_api", "label_en": "LLM Service", "label_zh": "LLM服务"},
    {"id": "index_generation", "label_en": "Index Generation", "label_zh": "索引生成"},
    {"id": "injection", "label_en": "Context Injection", "label_zh": "上下文注入"},
    {"id": "mcp", "label_en": "MCP Queries", "label_zh": "MCP查询"},
    {"id": "summary", "label_en": "Summary System", "label_zh": "摘要系统"},
    {"id": "timeline", "label_en": "Timeline", "label_zh": "时间线"},
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
            "label_en": spec.label_en,
            "label_zh": spec.label_zh,
            "advanced": spec.advanced,
            "restart_scope": spec.restart_scope,
            "secret": spec.secret,
            "project_allowed": spec.project_allowed,
        }
        if spec.unit_en is not None:
            row["unit_en"] = spec.unit_en
            row["unit_zh"] = spec.unit_zh
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
