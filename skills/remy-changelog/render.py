"""
Template rendering helper for log-change skill.
Uses Jinja2 when available, falls back to str-based rendering.
"""

import json
import os
from datetime import datetime

JINJA2_AVAILABLE = False
try:
    import jinja2
    JINJA2_AVAILABLE = True
except ImportError:
    pass

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SKILL_DIR, "templates")
SCHEMA_FILE = os.path.join(SKILL_DIR, "output_schema.json")


def load_schema():
    with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def render_template(template_name, context):
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
    lines = []
    lines.append(f"# Change Log: {context.get('task_id', 'unknown')}")
    lines.append(f"> Generated: {context.get('date', datetime.now().strftime('%Y-%m-%d'))} "
                 f"| Status: {context.get('status', 'unknown')}")
    lines.append("")

    lines.append("## 1. Pre-Implementation Discussion (Q&A)")
    qa_pairs = context.get("qa_pairs", [])
    if qa_pairs:
        for qa in qa_pairs:
            lines.append(f"- **Q**: {qa['question']}")
            lines.append(f"- **A**: {qa['answer']}")
            lines.append(f"- **Decision**: {qa['decision']}")
    else:
        lines.append("N/A")
    lines.append("")

    lines.append("## 2. File-Level Modifications")
    for i, mod in enumerate(context.get("file_modifications", []), 1):
        lines.append("")
        lines.append(f"### 2.{i} {mod['file_path']}")
        lines.append(f"- **Modification Summary**: {mod['summary']}")
        lines.append(f"- **Reason**: {mod['reason']}")
        lines.append(f"- **Role in Data Flow**: {mod['role']}")
        lines.append("- **Ripple Effects**:")
        for effect in mod.get("ripple_effects", []):
            lines.append(f"    - {effect}")
        lines.append("- **Code Logic**:")
        lines.append(f"    - {mod.get('logic_explanation', '')}")
    lines.append("")

    lines.append("## 3. Systemic Impact Analysis")
    impact = context.get("systemic_impact", {})
    lines.append(f"- **Data Flow**: {impact.get('data_flow', '')}")
    lines.append(f"- **Functional Hierarchy**: {impact.get('functional_hierarchy', '')}")
    lines.append(f"- **Framework Impact**: {impact.get('framework_impact', '')}")
    lines.append(f"- **API Consistency**: {impact.get('api_consistency', '')}")
    lines.append(f"- **Performance**: {impact.get('performance', '')}")
    lines.append("")

    lines.append("## 4. Verification Status")
    vs = context.get("verification_status", {})
    tests = vs.get("tests_passed", [])
    checks = vs.get("manual_checks", [])
    if tests:
        for t in tests:
            lines.append(f"- [x] Tests Passed: {t}")
    else:
        lines.append("- [ ] Tests Passed: (none)")
    if checks:
        for c in checks:
            lines.append(f"- [x] Manual Check: {c}")
    else:
        lines.append("- [ ] Manual Check: (none)")

    return "\n".join(lines) + "\n"


def save_changelog(project_root, context):
    log_dir = os.path.join(project_root, ".claude", "temp_log")
    os.makedirs(log_dir, exist_ok=True)

    task_id = context.get("task_id", "unknown")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    context.setdefault("date", datetime.now().strftime("%Y-%m-%d"))

    content = render_template("changelog.md.j2", context)

    filename = f"_temp_{task_id}_{timestamp}.md"
    filepath = os.path.join(log_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath
