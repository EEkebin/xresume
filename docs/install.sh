#!/bin/sh
# xresume installer for macOS and Linux.
#
#   curl -fsSL https://eekebin.github.io/xresume/install.sh | bash
#
# Puts one file, the `xresume` command, in ~/.local/bin. Run it again to update.
#
# It installs the standalone executable for your system when there is one (Linux x64 and arm64,
# macOS on Apple Silicon), which needs nothing else. Otherwise it installs the Python version,
# which needs Python 3.8 or newer.
#
#   XRESUME_BIN        folder to install into (default ~/.local/bin)
#   XRESUME_NO_BINARY  set to 1 to install the Python version even if an executable exists
#   XRESUME_RELEASES   where to fetch executables from (default: the latest GitHub release)
#   XRESUME_BASE       where to fetch xresume.py from (default: the GitHub repository)

set -eu

fetch() {   # fetch <url> <file>
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1"
    else
        echo "xresume: curl or wget is required." >&2
        exit 1
    fi
}

main() {
    releases="${XRESUME_RELEASES:-https://github.com/EEkebin/xresume/releases/latest/download}"
    base="${XRESUME_BASE:-https://raw.githubusercontent.com/EEkebin/xresume/main}"
    bin="${XRESUME_BIN:-$HOME/.local/bin}"
    mkdir -p "$bin"
    new="$bin/xresume.new"
    trap 'rm -f "$new"' EXIT

    # Never replace some other program that happens to be called xresume.
    if [ -e "$bin/xresume" ] && ! "$bin/xresume" --help 2>/dev/null | grep -q "resume a coding-agent conversation"; then
        echo "xresume: $bin/xresume already exists and is not this program. Nothing was changed." >&2
        echo "Move it away, or set XRESUME_BIN to another folder, and run this again." >&2
        exit 1
    fi

    asset=""
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) asset="xresume-linux-x64" ;;
        Linux-aarch64 | Linux-arm64) asset="xresume-linux-arm64" ;;
        Darwin-arm64) asset="xresume-macos-arm64" ;;
    esac

    installed=""
    if [ -n "$asset" ] && [ "${XRESUME_NO_BINARY:-}" != "1" ]; then
        echo "Downloading xresume..."
        # Keep it only if it actually runs here (an older system may not be able to run it).
        if fetch "$releases/$asset" "$new" 2>/dev/null && chmod 755 "$new" &&
           "$new" --help 2>/dev/null | grep -q "resume a coding-agent conversation"; then
            installed="standalone"
        else
            rm -f "$new"
            echo "No standalone executable for this system; installing the Python version."
        fi
    fi

    if [ -z "$installed" ]; then
        py=""
        for c in python3 python; do
            if command -v "$c" >/dev/null 2>&1 &&
               "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
                py="$c"
                break
            fi
        done
        if [ -z "$py" ]; then
            echo "xresume: Python 3.8 or newer is required and was not found. Install Python, then run this again." >&2
            exit 1
        fi
        tmp="$(mktemp)"
        fetch "$base/xresume.py" "$tmp" || { rm -f "$tmp"; echo "xresume: could not download from $base." >&2; exit 1; }
        # Refuse a truncated or wrong download before it replaces a working copy.
        if ! "$py" -c 'import ast, sys; s = open(sys.argv[1], encoding="utf-8").read(); ast.parse(s); sys.exit(0 if "def main" in s else 1)' "$tmp" >/dev/null 2>&1; then
            rm -f "$tmp"
            echo "xresume: the download from $base is not a valid copy of xresume. Nothing was changed." >&2
            exit 1
        fi
        # Run with the Python that was found (some systems only have `python`).
        { printf '#!/usr/bin/env %s\n' "$py"; tail -n +2 "$tmp"; } > "$new"
        rm -f "$tmp"
        chmod 755 "$new"
        installed="Python"
    fi

    mv -f "$new" "$bin/xresume"
    echo "Installed $bin/xresume ($installed version)"
    case ":$PATH:" in
        *":$bin:"*) echo "Run: xresume" ;;
        *)
            echo
            echo "$bin is not on your PATH. Add it by putting this line in your shell profile"
            echo "(~/.bashrc, ~/.zshrc or similar), then open a new terminal:"
            echo
            echo "    export PATH=\"$bin:\$PATH\""
            ;;
    esac
}

main
