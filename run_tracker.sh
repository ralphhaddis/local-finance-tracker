#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
PYTHON=".venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.10 or newer is required. Install Python and run this script again." >&2
    exit 1
  fi
  python3 -m venv .venv
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "local-finance-tracker requires Python 3.10 or newer. Recreate .venv with a supported Python version." >&2
  exit 1
fi

if ! "$PYTHON" -c 'from importlib.metadata import version; from pathlib import Path; import sys; reqs=[line.strip().split("==", 1) for line in Path("requirements.txt").read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]; sys.exit(0 if all(version(name) == wanted for name, wanted in reqs) else 1)' 2>/dev/null; then
  "$PYTHON" -m pip install -r requirements.txt
fi
"$PYTHON" src/setup.py

if command -v xdg-open >/dev/null 2>&1; then
  (sleep 2; xdg-open "http://127.0.0.1:8765" >/dev/null 2>&1) &
elif command -v sensible-browser >/dev/null 2>&1; then
  (sleep 2; sensible-browser "http://127.0.0.1:8765" >/dev/null 2>&1) &
else
  echo "Open http://127.0.0.1:8765 in your browser."
fi

exec "$PYTHON" src/web.py
