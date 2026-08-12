#!/bin/sh
set -e

REPO_URL="${REMY_REPO_URL:-https://github.com/MyPeacefulValentine/Remy-CC.git}"
BRANCH="${REMY_BRANCH:-main}"
MODE="install"
NON_INTERACTIVE=0
JSON_MODE=0
PURGE_STATE=0

usage() {
    printf 'Usage: %s [--update | --uninstall] [--non-interactive] [--json] [--purge-state]\n' "$(basename "$0")"
    printf '\n'
    printf 'Options:\n'
    printf '  (none)             Install Remy-CC\n'
    printf '  --update           Reinstall latest version\n'
    printf '  --uninstall        Remove Remy-CC\n'
    printf '  --non-interactive  Disable installer prompts\n'
    printf '  --json             Emit one JSON result object; implies --non-interactive\n'
    printf '  --purge-state      With --uninstall, remove user-level engine state\n'
    printf '  --help             Show this message\n'
    exit 0
}

die() {
    if [ "$JSON_MODE" -eq 1 ]; then
        operation="install"
        if [ "$MODE" = "update" ]; then
            operation="update"
        elif [ "$MODE" = "uninstall" ]; then
            operation="uninstall"
        fi
        printf '{"changed":[],"exit_code":1,"hook_mode":null,"operation":"%s","recovery":null,"schema_version":1,"status":"preflight_rejected","warnings":["installer entry preflight failed"]}\n' "$operation"
    else
        printf '[ERROR] %s\n' "$1" >&2
    fi
    exit 1
}

log() {
    if [ "$JSON_MODE" -eq 1 ]; then
        printf '%s\n' "$1" >&2
    else
        printf '%s\n' "$1"
    fi
}

find_python() {
    if command -v python3 >/dev/null 2>&1; then
        printf '%s\n' python3
    elif command -v python >/dev/null 2>&1; then
        printf '%s\n' python
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
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM

    log "[*] Cloning Remy-CC ($BRANCH)..."
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$tmp_dir/remy-cc" 2>/dev/null \
        || die "Failed to clone repository. Check network and URL: $REPO_URL"

    set -- "$PYTHON" "$tmp_dir/remy-cc/install.py"
    if [ "$MODE" = "uninstall" ]; then
        set -- "$@" --uninstall
    fi
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        set -- "$@" --non-interactive
    fi
    if [ "$JSON_MODE" -eq 1 ]; then
        set -- "$@" --json
    fi
    if [ "$PURGE_STATE" -eq 1 ]; then
        set -- "$@" --purge-state
    fi

    log '[*] Running installer...'
    if [ "$NON_INTERACTIVE" -eq 0 ] && [ -r /dev/tty ]; then
        "$@" < /dev/tty
    else
        "$@"
    fi
    log '[*] Cleanup complete.'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --update) MODE="update" ;;
        --uninstall) MODE="uninstall" ;;
        --non-interactive) NON_INTERACTIVE=1 ;;
        --json) JSON_MODE=1; NON_INTERACTIVE=1 ;;
        --purge-state) PURGE_STATE=1 ;;
        --help|-h) usage ;;
        *) die "Unknown option: $1. Use --help for usage." ;;
    esac
    shift
done

if [ "$PURGE_STATE" -eq 1 ] && [ "$MODE" != "uninstall" ]; then
    die "--purge-state requires --uninstall"
fi

check_deps
run_installer
