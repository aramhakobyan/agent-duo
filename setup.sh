#!/usr/bin/env bash
# Agent Duo setup: verify required executables (with min versions) and auth,
# then run the offline test suite. Changes nothing outside runs/ and
# workspace/; the codex binary is resolved at runtime (PATH, then the same
# fallback locations listed below), so config.json stays machine-independent.
#
# Portable: everything is resolved relative to this script, so the agent-duo
# folder can be dropped into any repository and set up with:  ./setup.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

MIN_PYTHON="3.9"
MIN_CLAUDE="2.0.0"
MIN_CODEX="0.45.0"   # needs `codex exec resume` + `-o/--json`

# Known fallback locations for codex when not on PATH
CODEX_FALLBACKS=(
  "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
  "$HOME/.local/bin/codex"
  "/opt/homebrew/bin/codex"
  "/usr/local/bin/codex"
)

OK=0; FAIL=1; status=$OK
pass() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✘\033[0m %s\n' "$1"; status=$FAIL; }

# version_ge A B -> 0 if A >= B (numeric, segment-wise; ignores -alpha suffixes)
version_ge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]
}

echo "Agent Duo setup — $(date '+%Y-%m-%d %H:%M')"
echo
echo "[1/4] Required executables"

# --- python3 ---
if command -v python3 >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if version_ge "$PYV" "$MIN_PYTHON"; then
    pass "python3 $PYV (>= $MIN_PYTHON)"
  else
    fail "python3 $PYV too old (need >= $MIN_PYTHON)"
  fi
else
  fail "python3 not found — install from https://www.python.org or 'brew install python'"
fi

# --- claude ---
if command -v claude >/dev/null 2>&1; then
  CLV="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  if [ -n "$CLV" ] && version_ge "$CLV" "$MIN_CLAUDE"; then
    pass "claude $CLV (>= $MIN_CLAUDE)"
  else
    fail "claude version '$CLV' too old or unreadable (need >= $MIN_CLAUDE) — update: 'claude update' or 'npm i -g @anthropic-ai/claude-code'"
  fi
else
  fail "claude not found — install: npm i -g @anthropic-ai/claude-code"
fi

# --- codex (PATH first, then known bundle locations) ---
CODEX_BIN=""
if command -v codex >/dev/null 2>&1; then
  CODEX_BIN="$(command -v codex)"
else
  for c in "${CODEX_FALLBACKS[@]}"; do
    if [ -x "$c" ]; then CODEX_BIN="$c"; break; fi
  done
fi
if [ -n "$CODEX_BIN" ]; then
  CXV="$("$CODEX_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  if [ -n "$CXV" ] && version_ge "$CXV" "$MIN_CODEX"; then
    pass "codex $CXV (>= $MIN_CODEX) at $CODEX_BIN"
  else
    fail "codex version '$CXV' too old or unreadable (need >= $MIN_CODEX) — update: npm i -g @openai/codex"
  fi
else
  fail "codex not found — install: npm i -g @openai/codex (or install the ChatGPT desktop app)"
fi

echo
echo "[2/4] Authentication"

if [ -f "$HOME/.claude.json" ] || [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -d "$HOME/.claude" ]; then
  pass "claude: credentials present (~/.claude or ANTHROPIC_API_KEY)"
else
  warn "claude: no credentials found — run 'claude' once to log in, or set ANTHROPIC_API_KEY"
fi

if [ -f "$HOME/.codex/auth.json" ] || [ -n "${OPENAI_API_KEY:-}" ]; then
  pass "codex: credentials present (~/.codex/auth.json or OPENAI_API_KEY)"
else
  warn "codex: no credentials found — run 'codex login' (or 'codex login --api-key ...')"
fi

echo
echo "[3/4] Project files"

for f in orchestrator.py server.py context_sources.py config.json roles/lead.md roles/challenger.md ui/index.html tests/test_agent_duo.py; do
  if [ -f "$SCRIPT_DIR/$f" ]; then pass "$f"; else fail "missing $f"; fi
done
mkdir -p "$SCRIPT_DIR/runs" "$SCRIPT_DIR/workspace"

if [ -f "$CONFIG" ] && ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$CONFIG" 2>/dev/null; then
  fail "config.json is not valid JSON"
fi

echo
echo "[4/4] Offline test suite (fake agents — no API calls, no cost)"

TEST_LOG="$(mktemp)"
if command -v python3 >/dev/null 2>&1 && \
   (cd "$SCRIPT_DIR" && python3 -m unittest discover -s tests >"$TEST_LOG" 2>&1); then
  pass "$(grep -E '^Ran ' "$TEST_LOG" || echo 'tests passed')"
else
  fail "tests failed — see output below"
  sed 's/^/    /' "$TEST_LOG" | tail -30
fi
rm -f "$TEST_LOG"

echo
if [ "$status" -eq "$OK" ]; then
  echo "All checks passed. Try it:"
  echo "  python3 '$SCRIPT_DIR/orchestrator.py' \"Hypothesis: ...\" --workdir <data dir>"
  echo "  python3 '$SCRIPT_DIR/server.py'        # web UI at http://127.0.0.1:8787"
else
  echo "Some checks FAILED — fix the ✘ items above and re-run ./setup.sh"
fi
exit "$status"
