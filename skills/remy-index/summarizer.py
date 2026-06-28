"""Summary generators for symbol / file / cluster layers.

Implements the three-tier length budget (soft / warn / retry) from the
v1.5.0 plan §5.3. Writes new ``summary_versions`` rows with strictly
monotonic ``version`` per ``(node_kind, node_ref)``.
"""
import json
import os
import re
from datetime import datetime


DEFAULT_LIMITS = {
    "symbol": 100,
    "file_cohesive": 250,
    "file_utility": 800,
    "cluster": 500,
}

_LEVEL_ENV = {
    "symbol": "SUMMARY_CHAR_LIMIT_SYMBOL",
    "file_cohesive": "SUMMARY_CHAR_LIMIT_FILE_COHESIVE",
    "file_utility": "SUMMARY_CHAR_LIMIT_FILE_UTILITY",
    "cluster": "SUMMARY_CHAR_LIMIT_CLUSTER",
}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def get_char_limit(level):
    env_name = _LEVEL_ENV.get(level)
    base = _env_int(env_name, DEFAULT_LIMITS[level]) if env_name else DEFAULT_LIMITS.get(level, 200)
    lang = os.environ.get("REMY_LANG", "en")
    if lang.startswith("zh"):
        factor = _env_float("SUMMARY_ZH_LENGTH_FACTOR", 0.5)
        return max(20, int(base * factor))
    return base


def _measure_payload(payload):
    if not isinstance(payload, dict):
        return 0
    short = payload.get("short") or ""
    full = payload.get("full") or ""
    return max(len(short), len(full))


def _length_verdict(measured, soft_limit):
    warn_limit = soft_limit * 1.2
    retry_limit = soft_limit * 1.5
    if measured <= warn_limit:
        return "ok"
    if measured <= retry_limit:
        return "oversized_warn"
    return "over_retry"


def _try_parse_payload(raw):
    if not isinstance(raw, str) or raw.startswith("Error:"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def generate_with_limit(level, llm_call, render_prompt, retry_strict_prompt):
    soft_limit = get_char_limit(level)
    raw_first = llm_call(render_prompt(soft_limit))
    payload_first = _try_parse_payload(raw_first)
    if payload_first is None:
        if raw_first is None or (isinstance(raw_first, str) and raw_first.startswith("Error:")):
            return None, "pending"
        return None, "corrupt"

    verdict = _length_verdict(_measure_payload(payload_first), soft_limit)
    if verdict == "ok":
        return payload_first, "ok"
    if verdict == "oversized_warn":
        return payload_first, "oversized_warn"

    raw_second = llm_call(retry_strict_prompt(soft_limit))
    payload_second = _try_parse_payload(raw_second)
    if payload_second is None:
        return payload_first, "oversized_hard"
    verdict_second = _length_verdict(_measure_payload(payload_second), soft_limit)
    if verdict_second == "ok":
        return payload_second, "ok"
    return payload_second, "oversized_hard"


def write_summary_version(db, node_kind, node_ref, payload, status,
                          decision_rationale=None, decision_dimension=None, decision_confidence=None):
    row = db.execute(
        "SELECT MAX(version) FROM summary_versions WHERE node_kind = ? AND node_ref = ?",
        (node_kind, node_ref),
    ).fetchone()
    next_version = (row[0] or 0) + 1
    summary_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    db.execute(
        "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, "
        "decision_rationale, decision_dimension, decision_confidence, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            node_kind,
            node_ref,
            next_version,
            summary_json,
            status,
            decision_rationale,
            decision_dimension,
            decision_confidence,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()
    if status == "ok":
        _bump_parent_counter_if_applicable(db, node_kind, node_ref)
    return next_version


def _bump_parent_counter_if_applicable(db, child_kind, child_ref):
    """Increment child_change_count on parent node when parent has an ok summary.

    Resolves child→parent by structural convention:
        symbol (node_ref = 'file_path::name') -> file (parent_ref = 'file_path')
        file   (node_ref = 'file_path')       -> cluster via cluster_members
    Skips increment if parent lacks a status='ok' summary_versions row (parent is
    still in bootstrap state — counter bump would be meaningless).
    """
    parent_kind, parent_ref = _resolve_parent(db, child_kind, child_ref)
    if parent_kind is None or parent_ref is None:
        return
    has_ok = db.execute(
        "SELECT 1 FROM summary_versions "
        "WHERE node_kind = ? AND node_ref = ? AND status = 'ok' LIMIT 1",
        (parent_kind, parent_ref),
    ).fetchone()
    if not has_ok:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
    except Exception:
        pass
    try:
        db.execute(
            "INSERT OR IGNORE INTO node_change_counters "
            "(node_kind, node_ref, child_change_count, leaf_descendant_count) "
            "VALUES (?, ?, 0, 0)",
            (parent_kind, parent_ref),
        )
        db.execute(
            "UPDATE node_change_counters "
            "SET child_change_count = child_change_count + 1 "
            "WHERE node_kind = ? AND node_ref = ?",
            (parent_kind, parent_ref),
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _resolve_parent(db, child_kind, child_ref):
    if child_kind == "symbol":
        if "::" not in child_ref:
            return (None, None)
        return ("file", child_ref.rsplit("::", 1)[0])
    if child_kind == "file":
        row = db.execute(
            "SELECT c.name FROM clusters c "
            "JOIN cluster_members cm ON cm.cluster_id = c.id "
            "WHERE cm.file_path = ?",
            (child_ref,),
        ).fetchone()
        if not row:
            return (None, None)
        return ("cluster", row[0])
    return (None, None)


_CLUSTER_TAGS = {
    "zh-CN": {
        "tag_position": "[定位]",
        "tag_api": "[API]",
        "tag_deps": "[依赖]",
        "empty_inbound_phrase": "无外部调用方",
    },
    "en": {
        "tag_position": "[Role]",
        "tag_api": "[API]",
        "tag_deps": "[Inbound]",
        "empty_inbound_phrase": "No external callers.",
    },
}


def _resolve_cluster_tags():
    lang = os.environ.get("REMY_LANG", "en")
    return _CLUSTER_TAGS.get(lang, _CLUSTER_TAGS["en"])


def _render_template(template_name, payload, char_limit, strict_note=""):
    prompts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
    path = os.path.join(prompts_dir, template_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return (
            f"Summarize: {json.dumps(payload, ensure_ascii=False)} "
            f"(max {char_limit} chars). {strict_note}"
        )
    kind_hint = payload.get("kind_hint", "") if isinstance(payload, dict) else ""
    text = _resolve_kind_conditionals(text, kind_hint)
    short_limit = min(80, char_limit)
    text = text.replace("{{char_limit}}", str(char_limit))
    text = text.replace("{{char_limit_short}}", str(short_limit))
    text = text.replace("{{char_limit_full}}", str(char_limit))
    text = text.replace("{{strict_note}}", strict_note)
    text = text.replace("{{kind_hint}}", kind_hint)
    text = text.replace("{{payload}}", json.dumps(payload, ensure_ascii=False, indent=2))
    cluster_tags = _resolve_cluster_tags()
    for placeholder, value in cluster_tags.items():
        text = text.replace("{{" + placeholder + "}}", value)
    return text


_IF_BLOCK_RE = re.compile(
    r"\{%\s*if\s+kind_hint\s*==\s*['\"]([^'\"]+)['\"]\s*%\}(.*?)\{%\s*endif\s*%\}",
    re.DOTALL,
)


def _resolve_kind_conditionals(text, kind_hint):
    def replace(match):
        condition_kind = match.group(1)
        block = match.group(2)
        return block if condition_kind == kind_hint else ""
    return _IF_BLOCK_RE.sub(replace, text)


def _file_input(db, file_path, kind_hint):
    sym_rows = db.execute(
        """SELECT s.name,
                  (SELECT summary FROM summary_versions sv
                   WHERE sv.node_kind='symbol' AND sv.node_ref = ? || s.name
                   ORDER BY sv.version DESC LIMIT 1)
           FROM symbols s WHERE s.file_path = ?""",
        (f"{file_path}::", file_path),
    ).fetchall()
    symbol_summaries = []
    for name, summary_json in sym_rows:
        if not summary_json:
            continue
        try:
            data = json.loads(summary_json)
            symbol_summaries.append({"name": name, "short": data.get("short")})
        except (json.JSONDecodeError, TypeError):
            continue
    imports_row = db.execute(
        "SELECT imports FROM files WHERE path = ?", (file_path,)
    ).fetchone()
    imports_list = []
    if imports_row and imports_row[0]:
        try:
            imports_list = json.loads(imports_row[0])
        except (json.JSONDecodeError, TypeError):
            imports_list = []
    return {
        "file_path": file_path,
        "kind_hint": kind_hint or "cohesive",
        "symbol_summaries": symbol_summaries,
        "imports": imports_list,
    }


def _cluster_input(db, cluster_name):
    file_rows = db.execute(
        """SELECT cm.file_path,
                  (SELECT summary FROM summary_versions sv
                   WHERE sv.node_kind='file' AND sv.node_ref = cm.file_path
                   ORDER BY sv.version DESC LIMIT 1)
           FROM cluster_members cm
           JOIN clusters c ON cm.cluster_id = c.id
           WHERE c.name = ?""",
        (cluster_name,),
    ).fetchall()
    file_summaries = []
    for fp, summary_json in file_rows:
        if not summary_json:
            continue
        try:
            data = json.loads(summary_json)
            file_summaries.append({"file": fp, "short": data.get("short")})
        except (json.JSONDecodeError, TypeError):
            continue
    entry_row = db.execute(
        "SELECT entry_symbols FROM clusters WHERE name = ?", (cluster_name,)
    ).fetchone()
    entry_symbols = []
    if entry_row and entry_row[0]:
        try:
            entry_symbols = json.loads(entry_row[0])
        except (json.JSONDecodeError, TypeError):
            entry_symbols = []
    inbound_rows = db.execute(
        """SELECT DISTINCT outer_c.name FROM clusters outer_c
           JOIN cluster_members om ON om.cluster_id = outer_c.id
           JOIN edges e ON e.source_file = om.file_path
           JOIN cluster_members im ON im.file_path = e.callee_file
           JOIN clusters inner_c ON inner_c.id = im.cluster_id
           WHERE inner_c.name = ? AND outer_c.name != ?""",
        (cluster_name, cluster_name),
    ).fetchall()
    return {
        "cluster_name": cluster_name,
        "file_summaries": file_summaries,
        "entry_symbols": entry_symbols,
        "inbound_clusters": [r[0] for r in inbound_rows],
    }


def summarize_file(db, file_path, kind_hint, llm_call):
    payload_in = _file_input(db, file_path, kind_hint)
    level = "file_utility" if kind_hint in ("low_cohesion", "utility") else "file_cohesive"

    def render(limit):
        return _render_template("summarize_file.md", payload_in, limit)

    def retry(limit):
        return _render_template(
            "summarize_file.md",
            payload_in,
            limit,
            strict_note=f"Output MUST be <= {limit} characters total.",
        )

    return generate_with_limit(level, llm_call, render, retry)


def summarize_cluster(db, cluster_name, llm_call):
    payload_in = _cluster_input(db, cluster_name)

    def render(limit):
        return _render_template("summarize_cluster.md", payload_in, limit)

    def retry(limit):
        return _render_template(
            "summarize_cluster.md",
            payload_in,
            limit,
            strict_note=f"Output MUST be <= {limit} characters total.",
        )

    return generate_with_limit("cluster", llm_call, render, retry)
