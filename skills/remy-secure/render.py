"""Template rendering helper for remy-secure skill."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).parent
TEMPLATES_DIR = SKILL_DIR / "templates"
SCHEMAS_DIR = SKILL_DIR / "schemas"

try:
    import jinja2
    _HAS_JINJA2 = True
except ImportError:
    _HAS_JINJA2 = False


def load_schema(name):
    path = SCHEMAS_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def render_template(template_name, context):
    if _HAS_JINJA2:
        return _render_jinja2(template_name, context)
    return _render_fallback(template_name, context)


def _render_jinja2(template_name, context):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template(template_name)
    return template.render(**context)


def _render_fallback(template_name, context):
    template_path = TEMPLATES_DIR / template_name
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    content = re.sub(r"\{\{-?\s*(\w+)\s*-?\}\}", lambda m: str(context.get(m.group(1), "")), content)

    lines = content.split("\n")
    output = []
    skip_until_endif = 0
    skip_until_endfor = 0

    for line in lines:
        if "{% for " in line:
            match = re.search(r"\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}", line)
            if match:
                item_name = match.group(1)
                list_name = match.group(2)
                items = context.get(list_name, [])
                block_lines = []
                i = lines.index(line) + 1
                depth = 1
                while i < len(lines) and depth > 0:
                    if "{% for " in lines[i]:
                        depth += 1
                    if "{% endfor %}" in lines[i]:
                        depth -= 1
                        if depth == 0:
                            break
                    block_lines.append(lines[i])
                    i += 1
                for idx, item in enumerate(items):
                    for bl in block_lines:
                        rendered = bl.replace("{{ loop.index }}", str(idx + 1))
                        if isinstance(item, dict):
                            for k, v in item.items():
                                rendered = rendered.replace("{{ " + f"{item_name}.{k}" + " }}", str(v))
                        output.append(rendered)
                skip_until_endfor = i
            continue
        if "{% endfor %}" in line:
            continue
        if lines.index(line) <= skip_until_endfor and skip_until_endfor > 0:
            continue
        if "{% if " in line:
            match = re.search(r"\{%\s*if\s+(?:not\s+)?(\w+)\s*%\}", line)
            if match:
                var_name = match.group(1)
                negate = "not " in line
                val = context.get(var_name)
                truthy = bool(val)
                if negate:
                    truthy = not truthy
                if not truthy:
                    skip_until_endif += 1
            continue
        if "{% else %}" in line:
            continue
        if "{% endif %}" in line:
            if skip_until_endif > 0:
                skip_until_endif -= 1
            continue
        if skip_until_endif > 0:
            continue
        output.append(line)

    return "\n".join(output)


def save_report(project_root, context):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    context.setdefault("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    report_content = render_template("report.md.j2", context)

    report_dir = Path(project_root) / ".claude" / "temp_test"
    report_dir.mkdir(parents=True, exist_ok=True)

    report_path = report_dir / f"security_audit_{timestamp}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    return str(report_path)
