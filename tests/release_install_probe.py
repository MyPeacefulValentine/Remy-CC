"""Exercise a release binary's install/verify path in an isolated temporary home."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run(binary, arguments, env, cwd):
    result = subprocess.run([str(binary), *arguments], env=env, cwd=cwd,
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode:
        raise RuntimeError(f"{arguments}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def snapshot(home):
    return {str(path.relative_to(home)): digest(path) for path in home.rglob("*") if path.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    args = parser.parse_args()
    binary = Path(args.binary).resolve(strict=True)
    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    scratch = Path(tempfile.mkdtemp(prefix="remy_release_install_"))
    home, work = scratch / "home", scratch / "work"
    claude, remy = home / ".claude", home / ".remy-cc"
    claude.mkdir(parents=True)
    work.mkdir()
    print(f"Isolated install root: {scratch}", flush=True)
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("REMY_") or key in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(key, None)
    env.update(HOME=str(home), USERPROFILE=str(home), CLAUDE_CONFIG_DIR=str(claude),
               REMY_CC_HOME=str(remy), REMY_DAEMON_AUTOSTART="false")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    settings_path = claude / "settings.json"
    settings_path.write_text(json.dumps({"env": {"USER_SENTINEL": "keep"},
                                         "permissions": {"allow": ["Read(synthetic.txt)"]},
                                         "outputStyle": "user-style"}), encoding="utf-8")
    user_rule = claude / "rules" / "user_local.md"
    user_rule.parent.mkdir()
    user_rule.write_text("Keep this user rule.\n", encoding="utf-8")
    user_bytes = user_rule.read_bytes()
    mcp_path = home / ".claude.json"
    other_server = {"command": "synthetic-server", "args": []}
    mcp_path.write_text(json.dumps({"mcpServers": {"user-server": other_server}}), encoding="utf-8")
    assert run(binary, ["--version"], env, work).strip() == f"remy-cc {expected}"
    run(binary, ["install", "--non-interactive", "--lang", "en"], env, work)
    installed = remy / "bin" / ("remy-cc.exe" if os.name == "nt" else "remy-cc")
    assert digest(installed) == digest(binary)
    assert run(installed, ["--version"], env, work).strip() == f"remy-cc {expected}"
    run(installed, ["verify"], env, work)
    manifest = read_json(remy / "install" / "manifest.json")
    assert manifest["schema_version"] == 4 and manifest["suite_version"] == expected
    descriptions = read_json(ROOT / "skills" / "skill_descriptions.json")
    checked = (
        "style.md", "rules/communication.md", "rules/reasoning-and-decisions.md",
        "skills/remy-plan/SKILL.md", "skills/remy-plan/output_schema.json",
        "skills/remy-plan/audit_template_en.md", "skills/remy-plan/audit_template_zh.md",
        "skills/remy-patch/SKILL.md", "skills/remy-testgen/SKILL.md",
        "skills/remy-testgen/prompts/generate_behavioral.md",
        "skills/remy-testgen/prompts/generate_boundary.md",
        "skills/remy-testgen/prompts/generate_property.md",
    )
    for relative in checked:
        deployed = claude / relative
        expected_bytes = (ROOT / relative).read_bytes()
        if relative.endswith("/SKILL.md"):
            skill = relative.split("/")[1]
            lines = expected_bytes.decode("utf-8").splitlines(keepends=True)
            for index, line in enumerate(lines):
                if line.startswith("description:"):
                    lines[index] = f"description: {descriptions[skill]['en']}\n"
                    break
            expected_bytes = "".join(lines).encode("utf-8")
        assert deployed.read_bytes() == expected_bytes, relative
        record = next(item for item in manifest["files"]
                      if item["root"] == "claude" and item["path"] == relative)
        role = "claude_skill" if relative.startswith("skills/") else "claude_protocol"
        assert record["sha256"] == digest(deployed) and record["role"] == role
    for name in ("planning_contract.py", "planning_contract.schema.json", "planning_contract.md"):
        relative = f"skills/remy-plan/{name}"
        assert not (claude / relative).exists(), relative
        assert not any(item["root"] == "claude" and item["path"] == relative for item in manifest["files"])
    settings = read_json(settings_path)
    assert settings["env"]["USER_SENTINEL"] == "keep"
    assert "Read(synthetic.txt)" in settings["permissions"]["allow"]
    assert settings["outputStyle"] == "user-style"
    assert user_rule.read_bytes() == user_bytes
    servers = read_json(mcp_path)["mcpServers"]
    assert servers["user-server"] == other_server
    assert Path(servers["remy-index"]["command"]).resolve() == installed.resolve()
    before = snapshot(home)
    output = run(binary, ["install", "--non-interactive", "--lang", "en"], env, work)
    run(installed, ["verify"], env, work)
    assert snapshot(home) == before, "repeated installation changed content"
    assert "0 file(s) written" in output
    print(f"PASS: {expected} install, verify, payload hashes, user configuration, MCP and idempotence")


if __name__ == "__main__":
    main()
