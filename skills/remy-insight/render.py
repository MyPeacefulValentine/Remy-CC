"""
Template rendering helper for remy-insight skill.
Uses Jinja2 when available, falls back to string-based rendering.
Includes CLI entry point for programmatic report generation from JSON findings.
"""

import argparse
import json
import os
import sys
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

DIMENSIONS_WITH_MECHANISM = frozenset(("innovation",))


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
    if template_name == "section_innovation.md.j2":
        return _render_innovation_fallback(context)
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


def _render_finding_base(f, lines):
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


def _render_finding_mechanism(f, lines):
    mechanism = f.get("mechanism")
    if mechanism:
        lines.append("")
        lines.append(f"> **Mechanism**: {mechanism}")
    significance = f.get("significance")
    if significance:
        lines.append("")
        lines.append(f"> **Significance**: {significance}")


def _render_finding_verification(f, lines):
    vs = f.get("verified_status")
    if vs and vs != "not_verified":
        vd = f.get("vote_detail", "")
        detail = f" ({vd})" if vd else ""
        lines.append(f"- **Verification**: {vs}{detail}")
    xrefs = f.get("cross_refs", [])
    if xrefs:
        lines.append(f"- **See also**: {', '.join(xrefs)}")


def _render_innovation_fallback(ctx):
    return _render_mechanism_section_fallback("innovation", ctx)


def _render_mechanism_section_fallback(section_name, ctx):
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
        _render_finding_base(f, lines)
        _render_finding_mechanism(f, lines)
        _render_finding_verification(f, lines)
        lines.append("")

    return "\n".join(lines) + "\n"


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
        _render_finding_base(f, lines)
        _render_finding_verification(f, lines)
        lines.append("")

    return "\n".join(lines) + "\n"


def annotate_cross_references(sections_data):
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


def _group_flat_findings(data):
    mode = data.get("mode", "global")
    depth = data.get("depth", "standard")
    dimensions = data.get("dimensions", [])
    raw_findings = data.get("findings", [])
    raw_findings = [f for f in raw_findings if isinstance(f, dict)]
    summaries = data.get("summaries", {})

    dim_lookup = {d: d for d in dimensions}
    for d in dimensions:
        for title_key in SECTION_TITLES:
            if d == title_key:
                dim_lookup[title_key] = d

    by_section = {}
    for f in raw_findings:
        dim = f.get("dimension") or ""
        target_dim = dim_lookup.get(dim)
        if target_dim is None:
            for title_key in SECTION_TITLES:
                if dim == title_key:
                    target_dim = title_key
                    break
        if target_dim is not None:
            by_section.setdefault(target_dim, []).append(f)
        elif dim:
            by_section.setdefault(dim, []).append(f)

    for dim_name, summary_text in summaries.items():
        if isinstance(summary_text, list):
            summary_text = "\n\n".join(str(s) for s in summary_text)
        elif not isinstance(summary_text, str):
            summary_text = str(summary_text) if summary_text is not None else ""
        by_section[f"{dim_name}_summary"] = summary_text

    sections_config = [d for d in dimensions if d in by_section]
    if not sections_config:
        sections_config = [k for k in by_section if not k.endswith("_summary")]

    severity_counts = data.get("findings_by_severity", {})
    total_findings = len(raw_findings)
    adversarial = data.get("adversarial_results", {})

    metadata = {
        "mode": mode,
        "depth": depth,
        "timestamp": data.get("timestamp", datetime.now().isoformat()),
        "scope_description": data.get("scope_description", f"Full repository ({total_findings} findings)"),
        "active_section_names": sections_config,
        "agent_count": data.get("agent_count", {
            "analysis": data.get("analysis_agent_count", 0),
            "adversarial": data.get("adversarial_agent_count", 0),
            "total": data.get("total_agent_count", 0),
        }),
        "adversarial_enabled": bool(adversarial) or depth in ("standard", "deep"),
        "freshness_warnings": data.get("freshness_warnings", []),
        "skipped_sections": data.get("skipped_sections", []),
    }

    return mode, depth, sections_config, by_section, metadata


def main():
    parser = argparse.ArgumentParser(description="Render remy-insight report from JSON findings")
    parser.add_argument("--input", required=True, help="Path to findings JSON file")
    parser.add_argument("--output", required=True, help="Output markdown file path")
    parser.add_argument("--mode", default=None, help="Override mode (global/focus/compare)")
    parser.add_argument("--depth", default=None, help="Override depth (light/standard/deep)")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    if args.mode:
        data["mode"] = args.mode
    if args.depth:
        data["depth"] = args.depth

    mode, depth, sections_config, findings_by_section, metadata = _group_flat_findings(data)

    report_content = assemble_report(mode, depth, sections_config, findings_by_section, metadata)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report_content)

    print(f"Report written to {args.output} ({len(report_content)} bytes)")


if __name__ == "__main__":
    main()
