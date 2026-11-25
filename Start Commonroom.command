#!/bin/bash
# Developer launcher for running Commonroom from source on macOS (double-click it in Finder).
# Opens the development desktop app if it is installed (desktop/node_modules), else runs the
# service with Python and opens it in the browser.
set -u
url='http://127.0.0.1:8731'
root="$(cd "$(dirname "$0")" && pwd)"
export PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"

fail() {
    printf '\033[31m%s\033[0m\n' "$1" >&2
    exit 1
}

electron="$root/desktop/node_modules/.bin/electron"
if [ -x "$electron" ]; then
    cd "$root/desktop" || fail "Could not open $root/desktop."
    nohup "$electron" . >/dev/null 2>&1 &
    exit 0
fi

if curl -fsS --max-time 1 "$url/api/health" 2>/dev/null | grep -q '"service": *"commonroom"'; then
    open "$url"
    exit 0
fi

# Newest first: a bare python3 is often Apple's older Command Line Tools Python (3.9).
python=''
for candidate in python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        python="$candidate"
        break
    fi
done
[ -n "$python" ] || fail 'Commonroom needs Python 3.10 or newer. Install it with: brew install python@3.12 (or from https://www.python.org/downloads/)'

echo "Starting Commonroom at $url (press Control-C or close this window to stop it)."
# Open the browser once the service answers; if it fails to start, its error stays in this window.
(for _ in $(seq 1 40); do
    if curl -fsS --max-time 1 "$url/api/health" 2>/dev/null | grep -q '"service": *"commonroom"'; then
        open "$url"
        exit 0
    fi
    sleep 0.25
done) &
cd "$root" || fail "Could not open $root."
exec "$python" -m commonroom --port 8731
