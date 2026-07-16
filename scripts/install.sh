#!/usr/bin/env bash
# install.sh — install LetsCode via `uv tool install git+...`
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/letscan/letscode/main/scripts/install.sh | sh
#
# Or clone and run locally:
#   ./scripts/install.sh
#
# What it does:
#   1. Ensures uv is present (bootstraps uv's official installer if missing)
#   2. Runs `uv tool install git+https://github.com/letscan/letscode.git`
#   3. Checks PATH, prints next steps if ~/.local/bin isn't on it
set -euo pipefail

# ---- Config ---------------------------------------------------------------
REPO="letscan/letscode"
BINARY="letscode"
GIT_URL="git+https://github.com/${REPO}.git"
UV_INSTALLER="https://astral.sh/uv/install.sh"

# ---- Colors (respect NO_COLOR / non-TTY) ----------------------------------
if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
    BLUE=''; GREEN=''; YELLOW=''; RED=''; BOLD=''; DIM=''; NC=''
else
    BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
    RED='\033[0;31m';  BOLD='\033[1m';     DIM='\033[2m';  NC='\033[0m'
fi

info()    { echo -e "${BLUE}▸${NC} ${1}"; }
success() { echo -e "${GREEN}✓${NC} ${1}"; }
error()   { echo -e "${RED}✗${NC} ${1}" >&2; }

# ---- OS check (uv handles arch internally) --------------------------------
OS="$(uname -s)"
case "$OS" in
    Linux*)  ;;
    Darwin*) ;;
    *) error "Unsupported OS: $OS (expected Linux or macOS)"; exit 1 ;;
esac

# ---- Step 1: ensure uv is present (bootstrap if missing) ------------------
if command -v uv >/dev/null 2>&1; then
    success "uv found ($(uv --version 2>/dev/null || echo 'installed'))"
else
    info "uv not found — installing it first..."
    # Delegate to uv's official installer (handles OS/arch/PATH/rc-files)
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$UV_INSTALLER" | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$UV_INSTALLER" | sh
    else
        error "Need curl or wget to bootstrap uv."
        exit 1
    fi
    # Pick up uv if the installer placed it in a default location
    for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        case ":${PATH}:" in
            *":${d}:"*) ;;
            *) [ -x "${d}/uv" ] && export PATH="${d}:${PATH}" ;;
        esac
    done
    command -v uv >/dev/null 2>&1 || {
        error "uv install failed. Open a new shell and re-run this script."
        exit 1
    }
    success "uv installed ($(uv --version))"
fi

# ---- Step 2: install LetsCode --------------------------------------------
info "Installing ${BOLD}${BINARY}${NC} from ${DIM}${GIT_URL}${NC}"
if ! uv tool install --force "$GIT_URL"; then
    error "Failed to install ${BINARY}. See output above."
    exit 1
fi
success "${BINARY} installed"

# ---- Step 3: PATH check + next steps -------------------------------------
# uv places tool entry points in ~/.local/bin (or $XDG_BIN_HOME) by default.
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

if [[ ":${PATH}:" == *":${BIN_DIR}:"* ]] || command -v "$BINARY" >/dev/null 2>&1; then
    success "Ready. Run: ${BOLD}${BINARY} --help${NC}"
else
    echo ""
    echo -e "${BOLD}Almost there${NC} — add ${DIM}${BIN_DIR}${NC} to your PATH:"
    SHELL_NAME="$(basename "${SHELL:-sh}")"
    case "$SHELL_NAME" in
        fish)
            echo -e "  ${BLUE}fish_add_path ${BIN_DIR}${NC}"
            echo -e "  ${DIM}(add to ~/.config/fish/config.fish to persist)${NC}" ;;
        zsh)
            echo -e "  ${BLUE}echo 'export PATH=\"${BIN_DIR}:\$PATH\"' >> ~/.zshrc && source ~/.zshrc${NC}" ;;
        bash)
            echo -e "  ${BLUE}echo 'export PATH=\"${BIN_DIR}:\$PATH\"' >> ~/.bashrc && source ~/.bashrc${NC}" ;;
        *)
            echo -e "  ${BLUE}export PATH=\"${BIN_DIR}:\$PATH\"${NC}" ;;
    esac
    echo ""
    echo -e "Then run: ${BOLD}${BINARY} --help${NC}"
fi

# CI convenience: expose on $GITHUB_PATH so the next step sees it
if [ -n "${GITHUB_PATH:-}" ]; then
    echo "$BIN_DIR" >> "$GITHUB_PATH"
fi

echo ""
echo -e "${GREEN}${BOLD}✨ LetsCode installed.${NC} Next: ${BOLD}cp config.example.json config.json${NC} and fill in your API key."
