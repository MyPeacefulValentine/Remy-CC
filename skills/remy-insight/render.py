"""
Template rendering helper for remy-insight skill.
Uses Jinja2 when available, falls back to string-based rendering.
"""

import json
import os
from collections import defaultdict
from datetime import datetime

JINJA2_AVAILABLE = False
try:
    import jinja2
    JINJA2_AVAILABLE = True
except ImportError:
    pass

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SKILL_DIR, "templates")
SCHEMAS_DIR = os.path.join(SKILL_DIR, "schemas")


def load_schema(name="agent_finding"):
    with open(os.path.join(SCHEMAS_DIR, f"{name}.json"), "r", encoding="utf-8") as f:
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
    if template_name == "_base.md.j2":
        return _render_base_fallback(context)
    if template_name == "section_executive.md.j2":
        return _render_executive_fallback(context)
    if template_name.startswith("section_") and template_name.endswith(".md.j2"):
        section_name = template_name[len("section_"):-len(".md.j2")]
        return _render_section_fallback(section_name, context)
    template_path = os.path.join(TEMPLATES_DIR, template_name)
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def _render_base_fallback(ctx):
    lines = []
    lines.append("# Repository Insight Report")
    lines.append(f"> Generated: {ctx.get('timestamp', '')}")
    lines.append(f"> Mode: {ctx.get('mode', '')} | Depth: {ctx.get('depth', '')}")
    lines.append(f"> Scope: {ctx.get('scope_description', '')}")
    lines.append("")

    warnings = ctx.get("freshness_warnings", [])
    if warnings:
        lines.append("> **Index Freshness Warnings:**")
        for w in warnings:
            lines.append(f"> - {w}")
        lines.append("")

    lines.append("---")
    lines.append("")

    for section in ctx.get("active_sections", []):
        lines.append(section.get("rendered_content", ""))
        lines.append("")
        lines.append("---")
        lines.append("")

    skipped = ctx.get("skipped_sections", [])
    if skipped:
        lines.append("## Skipped Sections")
        lines.append("")
        lines.append("| Section | Reason |")
        lines.append("| :--- | :--- |")
        for s in skipped:
            lines.append(f"| {s['name']} | {s['reason']} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    ac = ctx.get("agent_count", {})
    lines.append("## Methodology Notes")
    lines.append(f"- Agent count: {ac.get('analysis', 0)} analysis "
                 f"+ {ac.get('adversarial', 0)} adversarial "
                 f"= {ac.get('total', 0)} total")
    lines.append(f"- Depth: {ctx.get('depth', '')}")
    names = ctx.get("active_section_names", [])
    lines.append(f"- Active sections: {', '.join(names)}")
    adv = ctx.get("adversarial_enabled", False)
    lines.append(f"- Adversarial verification: {'enabled' if adv else 'disabled'}")

    return "\n".join(lines) + "\n"


def _render_executive_fallback(ctx):
    lines = ["## Executive Summary", ""]
    sections = ctx.get("sections", [])
    if not sections:
        lines.append("No analysis sections were completed.")
        return "\n".join(lines) + "\n"

    for section in sections:
        lines.append(f"### {section.get('name', 'unknown').replace('_', ' ').title()}")
        lines.append("")
        lines.append(section.get("summary", ""))
        lines.append("")
        findings = section.get("findings", [])
        if findings:
            issue_c = sum(1 for f in findings if f.get("severity") == "issue")
            concern_c = sum(1 for f in findings if f.get("severity") == "concern")
            obs_c = sum(1 for f in findings if f.get("severity") == "observation")
            lines.append("| Severity | Count |")
            lines.append("| :--- | :--- |")
            lines.append(f"| Issue | {issue_c} |")
            lines.append(f"| Concern | {concern_c} |")
            lines.append(f"| Observation | {obs_c} |")
        lines.append("")

    return "\n".join(lines) + "\n"


SECTION_TITLES = {
    "architecture": "Architecture Analysis",
    "innovation": "Technical Innovation",
    "improvement": "Improvement Roadmap",
    "robustness": "Robustness & Security",
    "doc_consistency": "Document-Code Consistency",
    "custom": "Custom Analysis",
}


def _render_section_fallback(section_name, ctx):
    title = SECTION_TITLES.get(section_name, section_name.replace("_", " ").title())
    lines = [f"## {title}", ""]
    lines.append(ctx.get("summary", ""))
    lines.append("")

    findings = ctx.get("findings", [])
    if not findings:
        lines.append(f"No {section_name} findings reported.")
        return "\n".join(lines) + "\n"

    lines.append("### Findings")
    lines.append("")

    for f in findings:
        lines.append(f"#### {f.get('id', 'F-???')}: {f.get('claim', '(no claim)')}")
        lines.append("")
        target = f.get("target", {})
        target_str = f"`{target.get('file', '')}`"
        if target.get("symbol"):
            target_str += f" → `{target['symbol']}`"
        if target.get("layer"):
            target_str += f" ({target['layer']})"
        lines.append(f"- **Severity**: {f.get('severity', '')} "
                     f"| **Confidence**: {f.get('confidence', '')}/5")
        lines.append(f"- **Target**: {target_str}")
        lines.append(f"- **Evidence**: {f.get('evidence', '')}")

        vs = f.get("verified_status")
        if vs and vs != "not_verified":
            vd = f.get("vote_detail", "")
            detail = f" ({vd})" if vd else ""
            lines.append(f"- **Verification**: {vs}{detail}")

        xrefs = f.get("cross_refs", [])
        if xrefs:
            lines.append(f"- **See also**: {', '.join(xrefs)}")

        lines.append("")

    return "\n".join(lines) + "\n"


def annotate_cross_references(sections_data):
    """Scan findings across sections and add cross_refs for shared targets."""
    target_index = defaultdict(list)
    for section in sections_data:
        for finding in section.get("findings", []):
            target = finding.get("target", {})
            key = (target.get("file", ""), target.get("symbol", ""))
            if key[0]:
                target_index[key].append((section["name"], finding.get("id", "")))

    for section in sections_data:
        for finding in section.get("findings", []):
            target = finding.get("target", {})
            key = (target.get("file", ""), target.get("symbol", ""))
            refs = target_index.get(key, [])
            if len(refs) < 2:
                continue
            cross = []
            finding_id = finding.get("id", "")
            for ref_section, ref_id in refs:
                if ref_section != section["name"] or ref_id != finding_id:
                    cross.append(f"§{ref_section} {ref_id}")
            if cross:
                finding["cross_refs"] = cross

    return sections_data


def assemble_report(mode, depth, sections_config, findings_by_section, metadata):
    """Assemble the full report from section findings and metadata."""
    sections_data = []
    for section_name in sections_config:
        section_findings = findings_by_section.get(section_name, [])
        summary = ""
        if section_findings:
            summary = findings_by_section.get(f"{section_name}_summary", "")
        sections_data.append({
            "name": section_name,
            "findings": section_findings,
            "summary": summary,
        })

    sections_data = annotate_cross_references(sections_data)

    rendered_sections = []
    accumulated_findings = []

    executive_ctx = {
        "sections": sections_data,
        "metadata": metadata,
    }
    executive_content = render_template("section_executive.md.j2", executive_ctx)
    rendered_sections.append({
        "name": "executive",
        "rendered_content": executive_content,
    })

    for section_name in sections_config:
        section = next((s for s in sections_data if s["name"] == section_name), None)
        if not section:
            continue
        template_file = f"section_{section_name}.md.j2"
        template_path = os.path.join(TEMPLATES_DIR, template_file)
        if not os.path.exists(template_path) and section_name not in SECTION_TITLES:
            continue
        context = {
            "findings": section["findings"],
            "summary": section["summary"],
            "upstream_findings": accumulated_findings.copy(),
            "metadata": metadata,
        }
        content = render_template(template_file, context)
        rendered_sections.append({
            "name": section_name,
            "rendered_content": content,
        })
        accumulated_findings.extend(section["findings"])

    rendered_names = [s["name"] for s in rendered_sections]
    base_ctx = {
        "active_sections": rendered_sections,
        "active_section_names": rendered_names,
        **metadata,
    }
    return render_template("_base.md.j2", base_ctx)


def save_report(project_root, mode, depth, sections_config, findings_by_section, metadata):
    """Render and save the insight report."""
    report_dir = os.path.join(project_root, ".claude", "temp_insight")
    os.makedirs(report_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    metadata = {**metadata}
    metadata.setdefault("timestamp", datetime.now().isoformat())

    report_content = assemble_report(
        mode, depth, sections_config, findings_by_section, metadata
    )

    report_path = os.path.join(report_dir, f"insight_{timestamp}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    return report_path
