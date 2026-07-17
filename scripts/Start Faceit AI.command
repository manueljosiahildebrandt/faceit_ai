#!/bin/bash
# Double-click in Finder to start Faceit AI (macOS).
# Keep this Terminal window open while the app is running; close it to stop.

set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

echo "Faceit AI — starting from: $ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: Python 3 is not installed or not on PATH."
  echo "Install Python 3.11–3.13 from https://www.python.org/downloads/ then try again."
  read -r -p "Press Enter to close…"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment (.venv)…"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Base app + optional Postgres driver (needed for shared DB URL / Test connection).
if ! python -c "import faceit_ai, psycopg" >/dev/null 2>&1; then
  echo "Installing Faceit AI (first run can take several minutes)…"
  python -m pip install --upgrade pip
  python -m pip install -e ".[postgres]"
fi

if [ ! -f config/default.yaml ]; then
  if [ -f config/default.example.yaml ]; then
    cp config/default.example.yaml config/default.yaml
    echo "Created config/default.yaml from example (edit via Settings in the browser)."
  fi
fi

echo "Starting web UI…"
exec faceit_ai_web
