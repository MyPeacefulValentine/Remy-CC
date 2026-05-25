#!/usr/bin/env python3
"""
Repository Audit Runner (Stage 2)
Core logic for the /remy-reposcout skill.
Handles cloning, analysis, and report generation in a sandboxed environment.
"""

import os
import sys
import shutil
import tempfile
import subprocess
import json
import stat
from pathlib import Path

# --- Configuration ---
MAX_TREE_DEPTH = 3
MAX_FILES_LIST = 100
MAX_SIZE_MB = 500  # 500MB safety limit

def run_command(cmd, cwd=None, check=False):
    """Run a shell command and return the result."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            shell=True,
            text=True,
            capture_output=True,
            check=check,
            encoding='utf-8',
            errors='replace'
        )
        return result
    except Exception as e:
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr=str(e))

def check_dependencies():
    """Verify git and gh are installed."""
    if run_command("git --version").returncode != 0:
        print("❌ Error: 'git' command not found. Please install Git.")
        sys.exit(1)

    if run_command("gh --version").returncode != 0:
        print("❌ Error: 'gh' command not found. Please install GitHub CLI (gh).")
        sys.exit(1)

def on_rm_error(func, path, exc_info):
    """
    Error handler for shutil.rmtree.
    If the error is due to an access error (read only file),
    it attempts to add write permission and then retries.
    If the error is because the file is open, it ignores the error.
    """
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        # Ignore other errors (like file in use)
        pass

def cleanup_directory(path):
    """Safely remove a directory tree."""
    path_obj = Path(path)
    if path_obj.exists():
        try:
            shutil.rmtree(path_obj, onerror=on_rm_error)
        except Exception as e:
            print(f"⚠️ Warning: Failed to fully clean up {path}: {e}")

def check_repo_size(repo_url):
    """Check repository size using gh api."""
    # Convert https://github.com/owner/repo to owner/repo
    try:
        if "github.com" in repo_url:
            parts = repo_url.split("github.com/")[-1].split("/")
            repo_slug = f"{parts[0]}/{parts[1]}".replace(".git", "")
        else:
            # Assume it's already owner/repo format if not a URL
            repo_slug = repo_url

        cmd = f"gh repo view {repo_slug} --json diskUsage,defaultBranchRef"
        result = run_command(cmd)

        if result.returncode != 0:
            print(f"⚠️ Warning: Could not fetch repo metadata: {result.stderr.strip()}")
            return None

        data = json.loads(result.stdout)
        size_kb = data.get("diskUsage", 0)
        size_mb = size_kb / 1024

        return size_mb
    except Exception as e:
        print(f"⚠️ Warning: Metadata check failed: {e}")
        return None

def analyze_tech_stack(repo_path):
    """Identify key technologies based on file presence."""
    stack = set()
    indicators = {
        "Node.js": ["package.json", "yarn.lock", "pnpm-lock.yaml"],
        "Python": ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "poetry.lock"],
        "Rust": ["Cargo.toml"],
        "Go": ["go.mod"],
        "Java": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "Docker": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
        "GitHub Actions": [".github/workflows"],
        "Terraform": ["main.tf", "variables.tf", "*.tf"],
        "C++": ["CMakeLists.txt", "Makefile"],
        "C#": ["*.csproj", "*.sln"],
        "PHP": ["composer.json"],
        "Ruby": ["Gemfile"],
    }

    for tech, patterns in indicators.items():
        for pattern in patterns:
            # Simple glob check
            if any(repo_path.glob(pattern)):
                stack.add(tech)
                break
            # Check for direct file existence if no glob wildcard
            if "*" not in pattern and (repo_path / pattern).exists():
                stack.add(tech)
                break

    return sorted(list(stack))

def generate_tree(repo_path, max_depth=MAX_TREE_DEPTH):
    """Generate a directory tree string."""
    tree_lines = []

    # Use walk to build tree
    for root, dirs, files in os.walk(repo_path):
        # Filter hidden dirs in-place
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        rel_path = Path(root).relative_to(repo_path)
        level = len(rel_path.parts)

        if str(rel_path) == ".":
            level = 0

        if level >= max_depth:
            continue

        indent = "  " * level
        if str(rel_path) != ".":
            tree_lines.append(f"{indent}📂 {rel_path.name}/")

        subindent = "  " * (level + 1)

        # Sort files/dirs
        dirs.sort()
        files.sort()

        count = 0
        for f in files:
            if f.startswith('.'): continue

            if count >= 15:  # Limit files per directory to keep output readable
                tree_lines.append(f"{subindent}... ({len(files)-15} more)")
                break
            tree_lines.append(f"{subindent}📄 {f}")
            count += 1

    return "\n".join(tree_lines[:MAX_FILES_LIST + 20]) # Buffer for dirs

def main():
    if len(sys.argv) < 2:
        print("Usage: python audit_runner.py <repo_url> [--force]")
        sys.exit(1)

    repo_url = sys.argv[1]
    force_clone = "--force" in sys.argv

    # Validate URL basic format
    if not repo_url.startswith("http") and "github.com" not in repo_url:
         # Also allow owner/repo format for gh cli compatibility if we wanted, but keeping http constraint for now is safer
         # unless we expand logic.
         pass

    check_dependencies()

    # Pre-flight check: Size
    print("📡 Fetching repository metadata...")
    size_mb = check_repo_size(repo_url)

    if size_mb is not None:
        print(f"📦 Repository Size: {size_mb:.2f} MB")
        if size_mb > MAX_SIZE_MB and not force_clone:
            print(f"\n❌ BLOCKED: Repository is too large (> {MAX_SIZE_MB}MB).")
            print("To override, run with --force flag.")
            sys.exit(1)

    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")

    # Use TEMP dir for cross-platform compatibility
    sandbox_base = Path(os.environ.get("REPO_AUDIT_ROOT", tempfile.gettempdir())) / "claude_audit"
    if not os.environ.get("REPO_AUDIT_ROOT"):
        # If no explicit root set, fallback to user home for better visibility (instead of temp)
        sandbox_base = Path.home() / "claude_audit"

    sandbox_base.mkdir(exist_ok=True)

    # Timestamped unique dir
    target_dir = sandbox_base / f"{repo_name}_{os.getpid()}"

    try:
        print(f"🏗️  Creating sandbox at {target_dir}...")
        cleanup_directory(target_dir) # Ensure clean start
        target_dir.mkdir()

        print(f"📥 Cloning {repo_url}...")
        # Shallow clone for speed
        clone_cmd = f"git clone --depth 1 {repo_url} ."
        clone_res = run_command(clone_cmd, cwd=target_dir)

        if clone_res.returncode != 0:
            print(f"❌ Clone failed:\n{clone_res.stderr}")
            # Cleanup failed directory
            cleanup_directory(target_dir)
            sys.exit(1)

        print("🔍 Analyzing repository structure...")
        tech_stack = analyze_tech_stack(target_dir)
        tree_view = generate_tree(target_dir)

        # Calculate actual local size
        total_size = 0
        file_count = 0
        for p in target_dir.rglob('*'):
            if p.is_file():
                total_size += p.stat().st_size
                file_count += 1

        local_size_mb = total_size / (1024 * 1024)

        # Format the output for Claude to read
        report = f"""
# 🕵️ Repository Audit: {repo_name}

## 📊 Overview
- **URL**: {repo_url}
- **Tech Stack**: {', '.join(tech_stack) if tech_stack else 'Unknown'}
- **Files**: {file_count}
- **Size (Local)**: {local_size_mb:.2f} MB
- **Local Path**: `{target_dir.absolute()}`

## 📂 Structure (Depth {MAX_TREE_DEPTH})
```text
{tree_view}
```

## 💡 Next Steps
Repository is ready for inspection.
Use `Glob`, `Grep`, or `Read` to explore specific files in `{target_dir.absolute()}`.

**Suggested Actions:**
1. Read README: `Read(file_path="{target_dir.absolute()}/README.md")`
2. Search logic: `Grep(pattern="TODO", path="{target_dir.absolute()}")`

**⚠️ CLEANUP**:
When finished, run:
`python -c "import shutil, os, stat; shutil.rmtree(r'{target_dir.absolute()}', onerror=lambda f, p, e: (os.chmod(p, stat.S_IWUSR), f(p)))"`
"""
        print(report)

    except Exception as e:
        print(f"❌ Error during audit: {str(e)}")
        if target_dir.exists():
            cleanup_directory(target_dir)
        sys.exit(1)

if __name__ == "__main__":
    main()
