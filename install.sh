#!/usr/bin/env sh
set -eu

# Zero-Python bootstrap installer for ULTRON (macOS / Linux).
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.sh | sh
#   ./install.sh [--project DIR] [--platforms claude,codex] [--update] [--uninstall]

ULTRON_REPO="${ULTRON_REPO:-https://github.com/ERamaM/ULTRON}"
ULTRON_REF="${ULTRON_REF:-main}"

prepend_path() {
    for bin_dir in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        case ":$PATH:" in
            *":$bin_dir:"*) ;;
            *) [ -d "$bin_dir" ] && PATH="$bin_dir:$PATH" ;;
        esac
    done
    export PATH
}

is_source_dir() {
    [ -f "$1/install.py" ] && [ -f "$1/skill/ultron/SKILL.md" ]
}

find_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c "import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >/dev/null 2>&1; then
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

download_to() {
    url="$1"
    dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$dest"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$dest" "$url"
    else
        echo "[ultron] Error: curl or wget is required to download ULTRON." >&2
        exit 1
    fi
}

install_uv() {
    echo "[ultron] Python 3.12+ with venv not found; bootstrapping standalone uv + Python 3.12..." >&2
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh >&2
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh >&2
    else
        echo "[ultron] Error: curl or wget is required to install uv." >&2
        exit 1
    fi
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
    if ! command -v uv >/dev/null 2>&1; then
        echo "[ultron] Error: uv installed, but 'uv' executable was not found on PATH ($HOME/.local/bin)." >&2
        exit 1
    fi
}

TMP_DIR=""
# shellcheck disable=SC2329 # invoked from a trap
cleanup() {
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT INT TERM

prepend_path

SRC_DIR=""
if [ -n "${0:-}" ] && [ -f "$0" ]; then
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    if is_source_dir "$SCRIPT_DIR"; then
        SRC_DIR="$SCRIPT_DIR"
    fi
fi
if [ -z "$SRC_DIR" ] && is_source_dir "$PWD"; then
    SRC_DIR="$PWD"
fi

if [ -z "$SRC_DIR" ]; then
    # --dev links the skill and an editable runtime to the source folder, but a downloaded source is removed
    # when this script exits. ("--de" is argparse's unambiguous abbreviation of --dev.)
    for arg in "$@"; do
        case "$arg" in
            --de|--dev)
                printf '%s\n' '{"error": "usage", "detail": "--dev needs a git checkout of ULTRON; clone the repository and run ./install.sh --dev there"}'
                exit 2
                ;;
        esac
    done
    if ! command -v tar >/dev/null 2>&1; then
        echo "[ultron] Error: 'tar' is required to unpack the ULTRON archive." >&2
        exit 1
    fi
    TMP_DIR=$(mktemp -d)
    ARCHIVE="$TMP_DIR/ultron.tar.gz"
    ARCHIVE_URL="${ULTRON_REPO}/archive/refs/heads/${ULTRON_REF}.tar.gz"
    echo "[ultron] Downloading ULTRON (${ULTRON_REF}) from ${ULTRON_REPO}..." >&2
    download_to "$ARCHIVE_URL" "$ARCHIVE"
    tar -xzf "$ARCHIVE" -C "$TMP_DIR"
    for dir in "$TMP_DIR"/*; do
        if [ -d "$dir" ] && is_source_dir "$dir"; then
            SRC_DIR="$dir"
            break
        fi
    done
    if [ -z "$SRC_DIR" ]; then
        echo "[ultron] Error: extracted archive did not contain install.py and skill/ultron/SKILL.md." >&2
        exit 1
    fi
fi

# Runs the installer as a background child so the shell can forward termination signals to it (a trap in
# the shell only runs after a foreground child exits) and still survive it, so the EXIT trap removes the
# downloaded source. fd 3 keeps the child's stdin: an asynchronous command otherwise gets /dev/null. A
# non-interactive shell starts background children with SIGINT ignored, so INT is forwarded as TERM; HUP
# too, so install.py stops a running pip/venv step before this shell removes the source it uses.
INSTALLER_PID=""
INSTALLER_SIGNALED=0
# shellcheck disable=SC2329 # invoked from a trap
forward_signal() {
    INSTALLER_SIGNALED=1
    if [ -n "$INSTALLER_PID" ]; then
        kill -s "$1" "$INSTALLER_PID" 2>/dev/null || true
    fi
}
run_installer() {
    { "$@" <&3 3<&- & } 3<&0
    INSTALLER_PID=$!
    trap 'forward_signal TERM' INT TERM HUP
    # A trapped signal interrupts wait, which then returns > 128 without the installer's status. Only then wait
    # again: the shell keeps the status of a background child until a wait reports it, so the next wait returns
    # the installer's own status (127 included), even if the child has already been reaped.
    while :; do
        INSTALLER_SIGNALED=0
        status=0
        wait "$INSTALLER_PID" || status=$?
        if [ "$INSTALLER_SIGNALED" -eq 0 ] || [ "$status" -le 128 ]; then
            break
        fi
    done
    INSTALLER_PID=""
    trap cleanup INT TERM
    trap - HUP
    return "$status"
}

# `|| rc=$?` keeps `set -e` from exiting before the installer's exit code is captured. The braces make the
# shell read the whole tail before running anything, so under `curl | sh` a child that reads stdin cannot
# swallow the final `exit` and turn a failure into exit 0.
{
    rc=0
    if command -v uv >/dev/null 2>&1; then
        run_installer uv run --python ">=3.12" --no-project "$SRC_DIR/install.py" "$@" || rc=$?
    elif PY_BIN=$(find_python); then
        run_installer "$PY_BIN" "$SRC_DIR/install.py" "$@" || rc=$?
    else
        install_uv
        run_installer uv run --python 3.12 --no-project "$SRC_DIR/install.py" "$@" || rc=$?
    fi
    exit "$rc"
}
