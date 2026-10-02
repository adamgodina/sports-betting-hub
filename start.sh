#!/bin/bash
# Starts the Odds Scanner server, setting it up on first run, and opens the board.
# Run from Terminal, or let Odds Scanner.app call it. Errors go to stderr and exit 1.

set -u
cd "$(dirname "$0")" || exit 1

URL="http://localhost:8765"
if [ "$(uname)" = Darwin ]; then LOG_DIR="$HOME/Library/Logs/OddsScanner"
else LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/odds-scanner"; fi
LOG="$LOG_DIR/server.log"
ENV_FILE="${ODDS_SCANNER_ENV:-$HOME/.config/odds-scanner/.env}"
VENV=.venv

fail() { echo "$1" >&2; exit 1; }
is_up() { curl -s -o /dev/null --max-time 1 "$URL/api/last"; }
open_path() { if [ "$(uname)" = Darwin ]; then open "$@"; else xdg-open "${@: -1}" >/dev/null 2>&1; fi; }
has_deps() { "$1" -c 'import requests, dotenv, cryptography' >/dev/null 2>&1; }

if is_up; then open_path "$URL"; exit 0; fi

# Keys: same lookup order as config._find_env, minus the override.
[ -f "$ENV_FILE" ] || [ -f .env ] || {
  mkdir -p "$(dirname "$ENV_FILE")" && cp .env.example "$ENV_FILE"
}
[ -f "$ENV_FILE" ] || ENV_FILE=.env
if ! grep -Eq '^ODDS_API_KEY=[^[:space:]]+' "$ENV_FILE"; then
  open_path -t "$ENV_FILE"
  fail "Add your Odds API key to $ENV_FILE (now open), save it, then start Odds Scanner again."
fi

# Finder gives apps a bare PATH, so look in the usual install locations too.
pick_python() {
  local p
  for p in "$(command -v python3 2>/dev/null)" /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
           "$HOME/.pyenv/shims/python3" /opt/anaconda3/bin/python3 "$HOME/anaconda3/bin/python3" \
           "$HOME/miniconda3/bin/python3" /opt/miniconda3/bin/python3 /usr/bin/python3; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    # Without the developer tools, /usr/bin/python3 is a stub that pops an installer.
    if [ "$p" = /usr/bin/python3 ] && [ "$(uname)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1 && { echo "$p"; return; }
  done
}

mkdir -p "$LOG_DIR"
PY="$VENV/bin/python3"
if ! has_deps "$PY"; then
  BASE="$(pick_python)"
  [ -n "$BASE" ] || fail "Odds Scanner needs Python 3.9 or newer. Install it from python.org, then start Odds Scanner again."
  echo "First run: installing packages into $VENV (about a minute)..."
  { [ -x "$PY" ] || "$BASE" -m venv "$VENV"; } >>"$LOG" 2>&1 \
    && "$PY" -m pip install -q -r requirements.txt >>"$LOG" 2>&1
  if ! has_deps "$PY"; then
    has_deps "$BASE" || fail "Installing packages failed. Details: $LOG"
    PY="$BASE"
  fi
fi

nohup "$PY" -m scanner.server >>"$LOG" 2>&1 </dev/null &
for _ in $(seq 1 40); do is_up && break; sleep 0.5; done
is_up || fail "The server didn't start. Details: $LOG"
open_path "$URL"
