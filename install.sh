#!/bin/sh
set -e

REPO_URL="${REMY_REPO_URL:-https://github.com/MyPeacefulValentine/Remy-CC.git}"
BRANCH="${REMY_BRANCH:-main}"

usage() {
    printf 'Usage: %s [--update | --uninstall | --help]\n' "$(basename "$0")"
    printf '\n'
    printf 'Options:\n'
    printf '  (none)        Install Remy-CC\n'
    printf '  --update      Reinstall latest version\n'
    printf '  --uninstall   Remove Remy-CC\n'
    printf '  --help        Show this message\n'
    exit 0
}

die() {
    printf '[ERROR] %s\n' "$1" >&2
    exit 1
}

find_python() {
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    elif command -v python >/dev/null 2>&1; then
        echo "python"
    else
        die "Python 3 is required but not found. Install Python 3.10+ and retry."
    fi
}

check_deps() {
    command -v git >/dev/null 2>&1 || die "git is required but not found. Install git and retry."
    PYTHON="$(find_python)"
    "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null \
        || die "Python 3.10+ is required. Current: $("$PYTHON" --version 2>&1)"
}

run_installer() {
    local mode="$1"
    local tmp_dir

    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT

    printf '[*] Cloning Remy-CC (%s)...\n' "$BRANCH"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$tmp_dir/remy-cc" 2>/dev/null \
        || die "Failed to clone repository. Check network and URL: $REPO_URL"

    printf '[*] Running installer...\n'
    case "$mode" in
        install)  "$PYTHON" "$tmp_dir/remy-cc/install.py" < /dev/tty ;;
        update)   "$PYTHON" "$tmp_dir/remy-cc/install.py" < /dev/tty ;;
        uninstall) "$PYTHON" "$tmp_dir/remy-cc/install.py" --uninstall < /dev/tty ;;
    esac

    printf '[*] Cleanup complete.\n'
}

MODE="install"
case "${1:-}" in
    --update)    MODE="update" ;;
    --uninstall) MODE="uninstall" ;;
    --help|-h)   usage ;;
    "")          MODE="install" ;;
    *)           die "Unknown option: $1. Use --help for usage." ;;
esac

check_deps
run_installer "$MODE"
