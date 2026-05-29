"""Template rendering helper for remy-testgen skill."""

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
FRAMEWORKS_FILE = os.path.join(SKILL_DIR, "frameworks.json")


def load_schema():
    schema_path = os.path.join(SKILL_DIR, "schemas", "test_scenario.json")
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_frameworks():
    with open(FRAMEWORKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return sorted(data["frameworks"], key=lambda x: x["priority"])


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
    if template_name.endswith(".md.j2"):
        return _render_report_fallback(context)
    if "python" in template_name:
        return _render_python_test_fallback(context)
    if "typescript" in template_name:
        return _render_ts_test_fallback(context)
    if "go" in template_name:
        return _render_go_test_fallback(context)
    if template_name == "test_c.c.j2":
        return _render_c_test_fallback(context)

    template_path = os.path.join(TEMPLATES_DIR, template_name)
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def _render_report_fallback(ctx):
    lines = []
    lines.append("# Test Generation Report")
    lines.append(f"> Generated: {ctx.get('timestamp', datetime.now().isoformat())}")
    lines.append(f"> Project: {ctx.get('project_name', 'unknown')}")
    lines.append(f"> Mode: {ctx.get('mode', 'post-hoc')}")
    lines.append(f"> Effort: {ctx.get('effort_level', 'medium')}")
    lines.append("")

    lines.append("## Target Set")
    lines.append("")
    lines.append("| File | Symbol | Type |")
    lines.append("| :--- | :--- | :--- |")
    for item in ctx.get("target_set", []):
        lines.append(f"| `{item['file']}` | `{item['symbol']}` | {item['type']} |")
    lines.append("")

    lines.append("## Test Plan")
    lines.append("")
    lines.append("| # | Symbol | Test Name | Category | Priority |")
    lines.append("| :--- | :--- | :--- | :--- | :--- |")
    for i, tp in enumerate(ctx.get("test_plan", []), 1):
        lines.append(f"| {i} | `{tp['symbol']}` | `{tp['test_name']}` "
                     f"| {tp['category']} | {tp['priority']} |")
    lines.append("")

    lines.append("## Generated Files")
    lines.append("")
    lines.append("| Path | Test Count |")
    lines.append("| :--- | :--- |")
    for gf in ctx.get("generated_files", []):
        lines.append(f"| `{gf['path']}` | {gf['test_count']} |")
    lines.append("")

    lines.append("## Test Results")
    lines.append("")
    lines.append("| Test | Status |")
    lines.append("| :--- | :--- |")
    for t in ctx.get("test_results", []):
        lines.append(f"| `{t['name']}` | {t['status']} |")
    lines.append("")
    lines.append(f"**Summary**: {ctx.get('passed', 0)}/{ctx.get('total', 0)} passed")
    lines.append("")

    coverage = ctx.get("coverage_data", [])
    if coverage:
        lines.append("## Branch Coverage")
        lines.append("")
        lines.append("| Symbol | Branches | Covered | Coverage | Status |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for c in coverage:
            lines.append(f"| `{c['symbol']}` | {c['branches']} | {c['covered']} "
                         f"| {c['percent']}% | {c['status']} |")
        lines.append("")
        lines.append(f"Supplement rounds: {ctx.get('supplement_rounds', 0)}")
        lines.append("")

    lines.append(f"## Status: {ctx.get('final_status', 'UNKNOWN')}")
    return "\n".join(lines) + "\n"


def _render_python_test_fallback(ctx):
    lines = [f'"""Tests for {ctx.get("module_name", "module")}."""']
    for imp in ctx.get("imports", []):
        lines.append(imp)
    lines.append("")
    for tc in ctx.get("test_cases", []):
        if tc.get("is_property"):
            lines.append(f"@given({tc.get('strategy', '')})")
            lines.append(f"def test_{tc['name']}({tc.get('params', '')}):")
        else:
            lines.append(f"def test_{tc['name']}():")
        lines.append(f'    """{tc.get("description", "")}"""')
        for body_line in tc.get("body_lines", []):
            lines.append(f"    {body_line}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_ts_test_fallback(ctx):
    lines = [f"// Tests for {ctx.get('module_name', 'module')}"]
    for imp in ctx.get("imports", []):
        lines.append(imp)
    lines.append("")
    for tc in ctx.get("test_cases", []):
        async_prefix = "async " if tc.get("is_async") else ""
        if tc.get("is_property"):
            arbs = tc.get("arbitraries", "")
            params = tc.get("params", "")
            lines.append(f"test.prop('{tc.get('description', '')}', [{arbs}], "
                         f"{async_prefix}({params}) => {{")
        else:
            lines.append(f"test('{tc.get('description', '')}', {async_prefix}() => {{")
        for body_line in tc.get("body_lines", []):
            lines.append(f"  {body_line}")
        lines.append("});")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_go_test_fallback(ctx):
    pkg = ctx.get("package_name", "main")
    lines = [f"// Tests for {pkg}"]
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


def _render_c_test_fallback(ctx):
    framework = ctx.get("framework", "plain_c")
    module = ctx.get("module_name", "module")
    suite = ctx.get("suite_name", "test_suite")
    includes = ctx.get("includes", [])
    test_cases = ctx.get("test_cases", [])

    lines = []

    if framework == "kunit":
        lines.append(f"// KUnit tests for {module}")
        lines.append("#include <kunit/test.h>")
        for inc in includes:
            lines.append(f"#include {inc}")
        lines.append("")
        for tc in test_cases:
            lines.append(f"static void {tc['name']}(struct kunit *test)")
            lines.append("{")
            for body_line in tc.get("body_lines", []):
                lines.append(f"\t{body_line}")
            lines.append("}")
            lines.append("")
        lines.append(f"static struct kunit_case {suite}_cases[] = {{")
        for tc in test_cases:
            lines.append(f"\tKUNIT_CASE({tc['name']}),")
        lines.append("\t{}")
        lines.append("};")
        lines.append("")
        lines.append(f"static struct kunit_suite {suite}_suite = {{")
        lines.append(f'\t.name = "{suite}",')
        lines.append(f"\t.test_cases = {suite}_cases,")
        lines.append("};")
        lines.append("")
        lines.append(f"kunit_test_suite({suite}_suite);")
        lines.append('MODULE_LICENSE("GPL");')

    elif framework == "cmocka":
        lines.append(f"// cmocka tests for {module}")
        lines.append("#include <stdarg.h>")
        lines.append("#include <stddef.h>")
        lines.append("#include <setjmp.h>")
        lines.append("#include <cmocka.h>")
        for inc in includes:
            lines.append(f"#include {inc}")
        lines.append("")
        for tc in test_cases:
            lines.append(f"static void {tc['name']}(void **state)")
            lines.append("{")
            lines.append("\t(void)state;")
            for body_line in tc.get("body_lines", []):
                lines.append(f"\t{body_line}")
            lines.append("}")
            lines.append("")
        lines.append("int main(void)")
        lines.append("{")
        lines.append("\tconst struct CMUnitTest tests[] = {")
        for tc in test_cases:
            lines.append(f"\t\tcmocka_unit_test({tc['name']}),")
        lines.append("\t};")
        lines.append("\treturn cmocka_run_group_tests(tests, NULL, NULL);")
        lines.append("}")

    elif framework == "unity":
        lines.append(f"// Unity tests for {module}")
        lines.append('#include "unity.h"')
        for inc in includes:
            lines.append(f"#include {inc}")
        lines.append("")
        lines.append("void setUp(void) {}")
        lines.append("void tearDown(void) {}")
        lines.append("")
        for tc in test_cases:
            lines.append(f"void {tc['name']}(void)")
            lines.append("{")
            for body_line in tc.get("body_lines", []):
                lines.append(f"\t{body_line}")
            lines.append("}")
            lines.append("")
        lines.append("int main(void)")
        lines.append("{")
        lines.append("\tUNITY_BEGIN();")
        for tc in test_cases:
            lines.append(f"\tRUN_TEST({tc['name']});")
        lines.append("\treturn UNITY_END();")
        lines.append("}")

    elif framework == "criterion":
        lines.append(f"// Criterion tests for {module}")
        lines.append("#include <criterion/criterion.h>")
        for inc in includes:
            lines.append(f"#include {inc}")
        lines.append("")
        for tc in test_cases:
            lines.append(f"Test({suite}, {tc['name']})")
            lines.append("{")
            for body_line in tc.get("body_lines", []):
                lines.append(f"\t{body_line}")
            lines.append("}")
            lines.append("")

    else:
        lines.append(f"// Tests for {module}")
        lines.append("#include <assert.h>")
        lines.append("#include <stdio.h>")
        for inc in includes:
            lines.append(f"#include {inc}")
        lines.append("")
        for tc in test_cases:
            lines.append(f"static int {tc['name']}(void)")
            lines.append("{")
            for body_line in tc.get("body_lines", []):
                lines.append(f"\t{body_line}")
            lines.append("\treturn 0;")
            lines.append("}")
            lines.append("")
        count = len(test_cases)
        lines.append("int main(void)")
        lines.append("{")
        lines.append("\tint failed = 0;")
        for tc in test_cases:
            lines.append(f'\tprintf("  {tc["name"]}... ");')
            lines.append(f"\tif ({tc['name']}() == 0) {{")
            lines.append('\t\tprintf("PASS\\n");')
            lines.append("\t} else {")
            lines.append('\t\tprintf("FAIL\\n");')
            lines.append("\t\tfailed++;")
            lines.append("\t}")
        lines.append(f'\tprintf("%d/%d passed\\n", {count} - failed, {count});')
        lines.append("\treturn failed ? 1 : 0;")
        lines.append("}")

    return "\n".join(lines) + "\n"


def save_report(project_root, context):
    report_dir = os.path.join(project_root, ".claude", "temp_testgen")
    os.makedirs(report_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    context.setdefault("timestamp", datetime.now().isoformat())
    report_content = render_template("report.md.j2", context)

    report_path = os.path.join(report_dir, f"testgen_{timestamp}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    return report_path


def save_coverage_report(project_root, context):
    report_dir = os.path.join(project_root, ".claude", "temp_testgen")
    os.makedirs(report_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    lines = []
    lines.append("# Coverage Report")
    lines.append(f"> Generated: {datetime.now().isoformat()}")
    lines.append(f"> Project: {context.get('project_name', 'unknown')}")
    lines.append(f"> Threshold: {context.get('threshold', 80)}%")
    lines.append("")

    lines.append("## Below Threshold")
    lines.append("")
    lines.append("| Symbol | Coverage | Uncovered Branches |")
    lines.append("| :--- | :--- | :--- |")
    for item in context.get("below_threshold", []):
        lines.append(f"| `{item['symbol']}` | {item['percent']}% "
                     f"| {item.get('uncovered', 'N/A')} |")
    lines.append("")

    suggestions = context.get("suggestions", [])
    if suggestions:
        lines.append("## Suggested Test Scenarios")
        lines.append("")
        for s in suggestions:
            lines.append(f"- `{s['symbol']}`: {s['description']}")
        lines.append("")

    content = "\n".join(lines) + "\n"
    report_path = os.path.join(report_dir, f"coverage_{timestamp}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)

    return report_path
