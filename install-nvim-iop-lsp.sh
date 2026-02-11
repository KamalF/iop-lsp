#!/usr/bin/env bash
#
# install-nvim-iop-lsp.sh — Set up iop-lsp for Neovim (≥ 0.11)
#
# Registers iop-lsp as an LSP server for .iop, .yaml, and .json files.
# Requires uv and Python ≥ 3.9.  Runs `uv sync` to ensure dependencies
# are installed.
#
# Usage:
#   ./install-nvim-iop-lsp.sh
#   ./install-nvim-iop-lsp.sh --uninstall

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $*"; }
error() { echo -e "${RED}[✗]${RESET} $*"; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIMRC="$HOME/.vimrc"

MARKER_BEGIN="\" ── BEGIN iop-lsp ──"
MARKER_END="\" ── END iop-lsp ──"

# ─── Uninstall ───────────────────────────────────────────────────────

if [[ "${1:-}" == "--uninstall" ]]; then
    echo -e "${BOLD}Uninstalling iop-lsp...${RESET}"

    if [[ -f "$VIMRC" ]] && grep -qF "$MARKER_BEGIN" "$VIMRC"; then
        sed -i "\|${MARKER_BEGIN}|,\|${MARKER_END}|d" "$VIMRC"
        info "Removed config block from $VIMRC"
    else
        warn "No iop-lsp config block found in $VIMRC"
    fi

    info "Done. Restart Neovim."
    exit 0
fi

# ─── Validate ────────────────────────────────────────────────────────

[[ ! -f "$REPO_DIR/pyproject.toml" ]] && { error "Missing $REPO_DIR/pyproject.toml."; exit 1; }
[[ ! -f "$VIMRC" ]] && { error "$VIMRC not found."; exit 1; }
command -v nvim &>/dev/null || { error "nvim not found in PATH."; exit 1; }
command -v uv &>/dev/null || { error "uv not found in PATH. Install it: https://docs.astral.sh/uv/"; exit 1; }

echo -e "${BOLD}Installing iop-lsp from $REPO_DIR${RESET}"
echo ""

# ─── 1. Locate tree-sitter-iop ───────────────────────────────────────

# Read current path from pyproject.toml
CURRENT_TS_PATH=$(sed -n 's/^tree-sitter-iop = { path = "\(.*\)" }$/\1/p' "$REPO_DIR/pyproject.toml")
# Resolve it relative to REPO_DIR for the default prompt
if [[ -n "$CURRENT_TS_PATH" ]]; then
    DEFAULT_TS_DIR=$(cd "$REPO_DIR" && realpath -m "$CURRENT_TS_PATH" 2>/dev/null || echo "$CURRENT_TS_PATH")
else
    DEFAULT_TS_DIR="$HOME/dev/github/tree-sitter-iop"
fi

echo -ne "Path to ${BOLD}tree-sitter-iop${RESET} repository [${DEFAULT_TS_DIR}]: "
read -r TS_INPUT
TS_DIR="${TS_INPUT:-$DEFAULT_TS_DIR}"

# Expand ~ and resolve to absolute path
TS_DIR="${TS_DIR/#\~/$HOME}"
TS_DIR=$(realpath -m "$TS_DIR")

if [[ ! -f "$TS_DIR/pyproject.toml" ]]; then
    error "No pyproject.toml found in $TS_DIR — is this a tree-sitter-iop checkout?"
    exit 1
fi
info "Using tree-sitter-iop at $TS_DIR"

# Compute relative path from REPO_DIR to TS_DIR
TS_REL=$(realpath --relative-to="$REPO_DIR" "$TS_DIR")

# Update pyproject.toml if the path changed
sed -i "s|^tree-sitter-iop = { path = \".*\" }$|tree-sitter-iop = { path = \"${TS_REL}\" }|" "$REPO_DIR/pyproject.toml"
info "Updated pyproject.toml: tree-sitter-iop path = \"$TS_REL\""

# ─── 2. Install dependencies ────────────────────────────────────────

echo ""
echo -e "Running ${BOLD}uv sync${RESET} ..."
if (cd "$REPO_DIR" && uv sync 2>&1 | tail -5); then
    info "Python dependencies installed."
else
    error "uv sync failed — check the output above."
    exit 1
fi

# ─── 3. Vimrc config block ──────────────────────────────────────────

if grep -qF "$MARKER_BEGIN" "$VIMRC" 2>/dev/null; then
    sed -i "\|${MARKER_BEGIN}|,\|${MARKER_END}|d" "$VIMRC"
fi

REPO_DIR_ESC="${REPO_DIR//\'/\\\'}"

cat >> "$VIMRC" << VIMRC_EOF

$MARKER_BEGIN
if has('nvim')
lua << EOF
  -- Start iop-lsp for .iop, .yaml, and .json files.
  vim.api.nvim_create_autocmd('FileType', {
    pattern = { 'iop', 'yaml', 'json' },
    callback = function()
      vim.lsp.start({
        name = 'iop-lsp',
        cmd = {
          'uv', 'run',
          '--project', '${REPO_DIR_ESC}',
          'python', '-m', 'iop_lsp', '--stdio',
        },
        root_dir = vim.fs.root(0, { '.git' }),
      })
    end,
  })
EOF
endif
$MARKER_END
VIMRC_EOF
info "Config block added to $VIMRC"

# ─── 4. Smoke test ──────────────────────────────────────────────────

echo ""
echo -e "Running smoke test..."
if timeout 10 bash -c "echo '' | uv run --project '${REPO_DIR}' python -m iop_lsp --stdio" >/dev/null 2>&1; then
    info "LSP server starts successfully."
else
    warn "Smoke test inconclusive — the server will be started by Neovim on demand."
fi

echo ""
echo -e "${BOLD}${GREEN}Done!${RESET} Restart Neovim and open a .iop, .yaml, or .json file."
