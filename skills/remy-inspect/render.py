"""
Template rendering helper for remy-inspect skill.
Uses Jinja2 when available, falls back to str.format()-based rendering.
"""

import json
import os
import re
from datetime import datetime

JINJA2_AVAILABLE = False
try:
    import jinja2
    JINJA2_AVAILABLE = True
except ImportError:
    pass

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SKILL_DIR, "templates")
FRAMEWORKS_FILE = os.path.join(SKILL_DIR, "frameworks.json")
ANTI_PATTERNS_FILE = os.path.join(SKILL_DIR, "anti_patterns.json")


def load_frameworks():
    """Load test framework detection rules from frameworks.json."""
    with open(FRAMEWORKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return sorted(data["frameworks"], key=lambda x: x["priority"])


def load_anti_patterns(languages=None):
    """Load assertion anti-pattern rules, optionally filtered by language."""
    with open(ANTI_PATTERNS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    patterns = data["patterns"]
    if languages:
        lang_set = set(languages)
        patterns = [p for p in patterns if lang_set & set(p.get("languages", []))]
    return patterns


def render_template(template_name, context):
    """Render a template file with the given context dict."""
    if JINJA2_AVAILABLE:
        return _render_jinja2(template_name, context)
    return _render_fallback(template_name, context)


def _render_jinja2(template_name, context):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_name)
    return template.render(**context)


def _render_fallback(template_name, context):
    """Minimal fallback renderer using regex substitution for simple Jinja2 patterns."""
    template_path = os.path.join(TEMPLATES_DIR, template_name)
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    if template_name.endswith(".md.j2"):
        return _render_report_fallback(context)
    if "python" in template_name:
        return _render_python_test_fallback(context)
    if "javascript" in template_name:
        return _render_js_test_fallback(context)
    if "go" in template_name:
        return _render_go_test_fallback(context)

    return content


def _render_report_fallback(ctx):
    lines = []
    lines.append("# Post-Verify Report")
    lines.append(f"> Generated: {ctx.get('timestamp', datetime.now().isoformat())}")
    lines.append(f"> Project: {ctx.get('project_name', 'unknown')}")
    lines.append(f"> Effort: {ctx.get('effort_level', 'medium')}")
    lines.append("")

    lines.append("## Change Set")
    lines.append("")
    lines.append("| File | Symbol | Type |")
    lines.append("| :--- | :--- | :--- |")
    for item in ctx.get("change_set", []):
        lines.append(f"| `{item['file']}` | `{item['symbol']}` | {item['type']} |")
    lines.append("")

    predictions = ctx.get("prediction_scenarios", [])
    if predictions:
        lines.append("## Defect Prediction")
        lines.append("")
        accuracy = ctx.get("prediction_accuracy", {})
        total_p = accuracy.get("total", len(predictions))
        confirmed_p = accuracy.get("confirmed_by_test", 0)
        lines.append(f"**Accuracy**: {confirmed_p}/{total_p} scenarios confirmed by test failures")
        lines.append("")
        lines.append("| Symbol | Category | Scenario | Priority | Test Result |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for p in predictions:
            lines.append(f"| `{p['symbol']}` | {p['category']} | {p['scenario']} "
                         f"| {p['priority']} | {p.get('test_result', 'N/A')} |")
        lines.append("")

    lines.append("## Test Discovery")
    lines.append("")
    lines.append("| Symbol | Existing Tests | Temporary Tests |")
    lines.append("| :--- | :--- | :--- |")
    for item in ctx.get("coverage_map", []):
        lines.append(f"| `{item['symbol']}` | {item['existing_count']} | {item['temp_count']} |")
    lines.append("")

    lines.append("## Test Results")
    lines.append("")
    lines.append("| Test | Status | Duration |")
    lines.append("| :--- | :--- | :--- |")
    for t in ctx.get("test_results", []):
        lines.append(f"| `{t['name']}` | {t['status']} | {t['duration']} |")
    lines.append("")
    lines.append(f"**Summary**: {ctx.get('passed', 0)}/{ctx.get('total', 0)} passed "
                 f"| Fix iterations: {ctx.get('fix_iterations', 0)}")
    lines.append("")

    lines.append("## Branch Coverage")
    lines.append("")
    lines.append("| Symbol | Branches | Covered | Coverage | Status |")
    lines.append("| :--- | :--- | :--- | :--- | :--- |")
    for c in ctx.get("coverage_data", []):
        lines.append(f"| `{c['symbol']}` | {c['branches']} | {c['covered']} "
                     f"| {c['percent']}% | {c['status']} |")
    lines.append("")

    lines.append("## Assertion Quality Audit")
    lines.append("")
    findings = ctx.get("audit_findings", [])
    if findings:
        lines.append("| ID | Pattern | Severity | File | Line | Source |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for a in findings:
            lines.append(f"| {a['id']} | {a['pattern_name']} | {a['severity']} "
                         f"| `{a['file']}` | {a['line']} | {a.get('source', 'regex')} |")
    else:
        lines.append("No anti-patterns detected.")
    lines.append("")

    semantic = ctx.get("semantic_findings", [])
    if semantic:
        lines.append("## Semantic Audit")
        lines.append("")
        lines.append("| Test | Category | Issue | Severity | Suggestion |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for s in semantic:
            lines.append(f"| `{s['test_name']}` | {s['category']} | {s['issue']} "
                         f"| {s['severity']} | {s.get('suggestion', '')} |")
        lines.append("")

    lines.append(f"## Status: {ctx.get('final_status', 'UNKNOWN')}")
    return "\n".join(lines) + "\n"


def _render_python_test_fallback(ctx):
    lines = [f'"""Temporary remy-inspect tests for {ctx.get("module_name", "module")}."""']
    for imp in ctx.get("imports", []):
        lines.append(imp)
    lines.append("")
    for tc in ctx.get("test_cases", []):
        lines.append(f"def test_{tc['name']}():")
        lines.append(f'    """{tc.get("description", "")}"""')
        for body_line in tc.get("body_lines", []):
            lines.append(f"    {body_line}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_js_test_fallback(ctx):
    lines = [f"// Temporary remy-inspect tests for {ctx.get('module_name', 'module')}"]
    for imp in ctx.get("imports", []):
        lines.append(imp)
    lines.append("")
    for tc in ctx.get("test_cases", []):
        async_prefix = "async " if tc.get("is_async") else ""
        lines.append(f"test('{tc.get('description', '')}', {async_prefix}() => {{")
        for body_line in tc.get("body_lines", []):
            lines.append(f"  {body_line}")
        lines.append("});")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_go_test_fallback(ctx):
    pkg = ctx.get("package_name", "main")
    lines = [f"// Temporary remy-inspect tests for {pkg}"]
    lines.append(f"package {pkg}_test")
    lines.append("")
    imports = ["testing"] + ctx.get("imports", [])
    lines.append("import (")
    for imp in imports:
        lines.append(f'\t"{imp}"')
    lines.append(")")
    lines.append("")
    for tc in ctx.get("test_cases", []):
        lines.append(f"func Test{tc['name']}(t *testing.T) {{")
        for body_line in tc.get("body_lines", []):
            lines.append(f"\t{body_line}")
        lines.append("}")
        lines.append("")
    return "\n".join(lines) + "\n"


def save_report(project_root, context):
    """Render and save the report to .claude/temp_test/."""
    report_dir = os.path.join(project_root, ".claude", "temp_test")
    os.makedirs(report_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    context.setdefault("timestamp", datetime.now().isoformat())
    report_content = render_template("report.md.j2", context)

    report_path = os.path.join(report_dir, f"report_{timestamp}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    return report_path
