#!/usr/bin/env bash
# Install the patient-extractor skill and MCP server for Claude Code at USER level, so they are
# available in every project (not only when Claude Code is opened in this repo).
#
#   scripts/install_claude_code.sh            install skill + register MCP server (user scope)
#   scripts/install_claude_code.sh --uninstall
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/patient-extractor"
NAME="patient-data-extractor"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$SKILL_DIR"
  claude mcp remove --scope user "$NAME" 2>/dev/null || true
  echo "Removed skill ($SKILL_DIR) and MCP server '$NAME'."
  exit 0
fi

[[ -x "$ROOT/.venv/bin/python" ]] || { echo "Create the venv first: uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt"; exit 1; }

# 1. Skill: copy, and rewrite the relative CLI path to this checkout's absolute path.
mkdir -p "$SKILL_DIR"
sed "s#bin/hdx#$ROOT/bin/hdx#g; s#Run commands from the repo root.#The CLI lives at $ROOT/bin/hdx and can be run from any directory.#" \
  "$ROOT/.claude/skills/patient-extractor/SKILL.md" > "$SKILL_DIR/SKILL.md"
echo "Skill installed: $SKILL_DIR/SKILL.md"

# 2. MCP server (optional; the skill works without it).
if command -v claude >/dev/null 2>&1; then
  claude mcp remove --scope user "$NAME" >/dev/null 2>&1 || true
  claude mcp add --scope user "$NAME" -- "$ROOT/bin/hdx-mcp"
  echo "MCP server '$NAME' registered at user scope."
else
  echo "Claude Code CLI not found; skipped MCP registration. Later run:"
  echo "  claude mcp add --scope user $NAME -- $ROOT/bin/hdx-mcp"
fi
