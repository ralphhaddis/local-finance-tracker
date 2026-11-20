#!/bin/zsh
LFT_PROJECT="$(cd "$(dirname "$0")" && pwd)"
cd "$LFT_PROJECT" || exit 1

PYTHON="./.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required. Install Python 3.10 or newer and try again."
    read -r "?Press Return to close..."
    exit 1
  fi
  python3 -m venv .venv || exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "local-finance-tracker requires Python 3.10 or newer. Recreate .venv with a supported Python version."
  read -r "?Press Return to close..."
  exit 1
fi

if ! "$PYTHON" -c 'from importlib.metadata import version; from pathlib import Path; import sys; reqs=[line.strip().split("==", 1) for line in Path("requirements.txt").read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]; sys.exit(0 if all(version(name) == wanted for name, wanted in reqs) else 1)' 2>/dev/null; then
  "$PYTHON" -m pip install -r requirements.txt || exit 1
fi
"$PYTHON" src/setup.py || exit 1

(sleep 2; open "http://127.0.0.1:8765") &
exec "$PYTHON" ./src/web.py
