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

if command -v uv >/dev/null 2>&1; then
    exec uv run --python ">=3.12" --no-project "$SRC_DIR/install.py" "$@"
fi

if PY_BIN=$(find_python); then
    exec "$PY_BIN" "$SRC_DIR/install.py" "$@"
fi

install_uv
exec uv run --python 3.12 --no-project "$SRC_DIR/install.py" "$@"
