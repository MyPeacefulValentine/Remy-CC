#!/bin/sh
set -e

REPO="${REMY_CC_REPO:-MyPeacefulValentine/Remy-CC}"
TAG="${REMY_CC_TAG:-}"
MODE="install"
NON_INTERACTIVE=0
PURGE_STATE=0
LANG_OPT=""

usage() {
    printf 'Usage: %s [--uninstall] [--lang en|zh-CN] [--non-interactive] [--purge-state]\n' "$(basename "$0")"
    printf '\n'
    printf 'Downloads the remy-cc release binary for this platform, verifies its\n'
    printf 'sha256 checksum, and hands over to `remy-cc install` (idempotent; a\n'
    printf 'rerun installs the latest release, `remy-cc update` self-updates).\n'
    printf '\n'
    printf 'Options:\n'
    printf '  (none)             Install Remy-CC\n'
    printf '  --uninstall        Remove Remy-CC via the installed binary\n'
    printf '  --lang en|zh-CN    Interface language for the deployed artifacts\n'
    printf '  --non-interactive  Skip prompts\n'
    printf '  --purge-state      With --uninstall, remove user-level engine state\n'
    printf '  --help             Show this message\n'
    printf '\n'
    printf 'Environment:\n'
    printf '  REMY_CC_REPO       GitHub repository slug (default: %s)\n' "$REPO"
    printf '  REMY_CC_TAG        Pin a release tag (default: latest release)\n'
    exit 0
}

die() {
    printf '[ERROR] %s\n' "$1" >&2
    exit 1
}

log() {
    printf '%s\n' "$1"
}

detect_target() {
    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os" in
        Linux)
            case "$arch" in
                x86_64 | amd64) TARGET="x86_64-unknown-linux-musl" ;;
                *) die "unsupported architecture: $os/$arch (release targets: x86_64 Linux, x86_64/arm64 macOS)" ;;
            esac
            ;;
        Darwin)
            case "$arch" in
                arm64) TARGET="aarch64-apple-darwin" ;;
                x86_64) TARGET="x86_64-apple-darwin" ;;
                *) die "unsupported architecture: $os/$arch" ;;
            esac
            ;;
        *) die "unsupported platform: $os. On Windows use install.ps1." ;;
    esac
}

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1"
    else
        die "curl or wget is required but neither was found."
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        die "sha256sum or shasum is required but neither was found."
    fi
}

resolve_tag() {
    if [ -n "$TAG" ]; then
        return
    fi
    api_url="https://api.github.com/repos/$REPO/releases/latest"
    fetch "$api_url" "$tmp_dir/latest.json" || die "cannot query the latest release: $api_url"
    TAG="$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$tmp_dir/latest.json" | head -n 1)"
    if [ -z "$TAG" ]; then
        die "cannot parse tag_name from the release metadata"
    fi
}

run_install() {
    command -v tar >/dev/null 2>&1 || die "tar is required but not found."
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM

    detect_target
    resolve_tag
    asset="remy-cc-$TAG-$TARGET.tar.gz"
    base_url="https://github.com/$REPO/releases/download/$TAG"

    log "[*] Downloading $asset ..."
    fetch "$base_url/$asset" "$tmp_dir/$asset" || die "download failed: $base_url/$asset"
    fetch "$base_url/$asset.sha256" "$tmp_dir/$asset.sha256" || die "checksum download failed: $base_url/$asset.sha256"

    expected="$(awk '{print $1}' "$tmp_dir/$asset.sha256")"
    actual="$(sha256_of "$tmp_dir/$asset")"
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        die "sha256 mismatch for $asset: expected '$expected', got '$actual'"
    fi
    log '[*] Checksum verified.'

    tar -xzf "$tmp_dir/$asset" -C "$tmp_dir" || die "archive extraction failed: $asset"
    if [ ! -f "$tmp_dir/remy-cc" ]; then
        die "archive did not contain the remy-cc binary"
    fi
    chmod +x "$tmp_dir/remy-cc"

    set -- "$tmp_dir/remy-cc" install
    if [ -n "$LANG_OPT" ]; then
        set -- "$@" --lang "$LANG_OPT"
    fi
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        set -- "$@" --non-interactive
    fi

    log '[*] Running remy-cc install ...'
    if [ "$NON_INTERACTIVE" -eq 0 ] && [ -r /dev/tty ]; then
        "$@" < /dev/tty
    else
        "$@"
    fi
    log '[*] Done.'
}

run_uninstall() {
    bin="${REMY_CC_HOME:-$HOME/.remy-cc}/bin/remy-cc"
    if [ ! -x "$bin" ]; then
        die "remy-cc binary not found at $bin; nothing to uninstall"
    fi
    set -- "$bin" uninstall
    if [ "$PURGE_STATE" -eq 1 ]; then
        set -- "$@" --purge-state
    fi
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        set -- "$@" --yes
    fi
    if [ "$NON_INTERACTIVE" -eq 0 ] && [ -r /dev/tty ]; then
        "$@" < /dev/tty
    else
        "$@"
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --uninstall) MODE="uninstall" ;;
        --lang)
            shift
            [ "$#" -gt 0 ] || die "--lang requires a value (en or zh-CN)"
            LANG_OPT="$1"
            ;;
        --non-interactive) NON_INTERACTIVE=1 ;;
        --purge-state) PURGE_STATE=1 ;;
        --help | -h) usage ;;
        *) die "Unknown option: $1. Use --help for usage." ;;
    esac
    shift
done

if [ "$PURGE_STATE" -eq 1 ] && [ "$MODE" != "uninstall" ]; then
    die "--purge-state requires --uninstall"
fi

if [ "$MODE" = "uninstall" ]; then
    run_uninstall
else
    run_install
fi
