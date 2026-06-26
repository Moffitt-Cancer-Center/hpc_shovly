#!/bin/bash
# setup.sh — First-time environment setup for Shovly (HPC Cloud Cost Comparator)
#
# Run this once after cloning the repository, on both:
#   1. The server that will run the FastAPI dashboard (Docker or bare-metal)
#   2. Each HPC login node that will run the Slurm agent via cron
#
# Usage:
#   bash setup.sh [--mode server|agent|both] [--non-interactive]
#
# Options:
#   --mode server        Only set up the dashboard server environment
#   --mode agent         Only set up the Slurm agent environment
#   --mode both          Set up both (default)
#   --non-interactive    Accept all defaults without prompting

set -euo pipefail

# ============================================================
# Colors / formatting
# ============================================================
if [ -t 1 ]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; CYAN=''; BOLD=''; RESET=''
fi

info()    { printf "${GREEN}[INFO]${RESET}  %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${RESET}  %s\n" "$*"; }
error()   { printf "${RED}[ERROR]${RESET} %s\n" "$*" >&2; }
section() { printf "\n${BOLD}${CYAN}=== %s ===${RESET}\n" "$*"; }
ok()      { printf "  ${GREEN}✓${RESET} %s\n" "$*"; }
skip()    { printf "  ${YELLOW}–${RESET} %s\n" "$*"; }
fail()    { printf "  ${RED}✗${RESET} %s\n" "$*"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ============================================================
# Argument parsing
# ============================================================
MODE="both"
INTERACTIVE=1

while [ "$#" -gt 0 ]; do
    case "$1" in
        --mode)           MODE="$2"; shift 2 ;;
        --non-interactive) INTERACTIVE=0; shift ;;
        -h|--help)
            echo "Usage: $0 [--mode server|agent|both] [--non-interactive]"
            exit 0 ;;
        *) error "Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ "$MODE" != "server" && "$MODE" != "agent" && "$MODE" != "both" ]]; then
    error "--mode must be server, agent, or both"
    exit 1
fi

# ============================================================
# Prompt helper (skipped in non-interactive mode)
# ============================================================
# prompt VAR_NAME "Question text" "default_value"
prompt() {
    local var_name="$1" question="$2" default="$3"
    if [ "$INTERACTIVE" -eq 0 ]; then
        eval "$var_name=\"\$default\""
        return
    fi
    local display_default=""
    [ -n "$default" ] && display_default=" [${default}]"
    printf "  %s%s: " "$question" "$display_default"
    read -r user_input
    eval "$var_name=\"\${user_input:-\$default}\""
}

# ============================================================
# Header
# ============================================================
printf "\n${BOLD}Shovly — HPC Cloud Cost Comparator${RESET}\n"
printf "Setup script  •  $(date '+%Y-%m-%d %H:%M:%S')\n"
printf "Working directory: %s\n\n" "$SCRIPT_DIR"

# ============================================================
# 1. Python 3 check
# ============================================================
section "Python"

PYTHON3="$(command -v python3 2>/dev/null || true)"
if [ -z "$PYTHON3" ]; then
    error "python3 not found in PATH. Install Python 3.8+ before continuing."
    exit 1
fi
PY_VERSION="$("$PYTHON3" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
ok "Found python3 at $PYTHON3 (version $PY_VERSION)"

# Warn if below 3.8 (server needs 3.8+; agent works on 3.6+)
PY_MINOR="$("$PYTHON3" -c 'import sys; print(sys.version_info[1])')"
if [ "$PY_MINOR" -lt 6 ]; then
    error "Python 3.6+ required. Found 3.$PY_MINOR"
    exit 1
elif [ "$PY_MINOR" -lt 8 ] && [[ "$MODE" == "server" || "$MODE" == "both" ]]; then
    warn "The dashboard server (app.py) requires Python 3.8+. Consider upgrading."
fi

# ============================================================
# 2. Directory structure
# ============================================================
section "Directory structure"

APP_DIR="$SCRIPT_DIR/hpc-cost-comparator"
DATA_DIR="$APP_DIR/data"
LOG_DIR="$APP_DIR/logs"

for d in "$APP_DIR" "$DATA_DIR" "$LOG_DIR"; do
    if [ -d "$d" ]; then
        ok "Exists: $d"
    else
        mkdir -p "$d"
        ok "Created: $d"
    fi
done

# ============================================================
# 3. Virtual environment
# ============================================================
section "Python virtual environment"

VENV_DIR="$SCRIPT_DIR/venv"

if [ -x "$VENV_DIR/bin/python3" ]; then
    ok "venv already exists at $VENV_DIR"
else
    info "Creating venv at $VENV_DIR ..."
    "$PYTHON3" -m venv "$VENV_DIR"
    ok "venv created"
fi

VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

# Install / upgrade dependencies
if [[ "$MODE" == "server" || "$MODE" == "both" ]]; then
    info "Installing server dependencies from requirements.txt ..."
    "$VENV_PIP" install --quiet --upgrade pip
    "$VENV_PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
    ok "Server dependencies installed (fastapi, uvicorn, pydantic, httpx)"
fi

if [[ "$MODE" == "agent" || "$MODE" == "both" ]]; then
    info "Installing agent dependency (requests) ..."
    "$VENV_PIP" install --quiet --upgrade pip 2>/dev/null || true
    "$VENV_PIP" install --quiet requests
    ok "Agent dependency installed (requests)"
fi

# ============================================================
# 4. Slurm environment (agent / both)
# ============================================================
if [[ "$MODE" == "agent" || "$MODE" == "both" ]]; then
    section "Slurm configuration"

    # --- Slurm bin dir ---
    DEFAULT_SLURM_BIN="/cm/shared/apps/slurm/current/bin"
    if [ -d "$DEFAULT_SLURM_BIN" ]; then
        DETECTED_SLURM_BIN="$DEFAULT_SLURM_BIN"
    else
        # Try to find sacct in PATH
        _sacct_path="$(command -v sacct 2>/dev/null || true)"
        DETECTED_SLURM_BIN="$( [ -n "$_sacct_path" ] && dirname "$_sacct_path" || echo '' )"
    fi

    prompt SLURM_BIN_DIR \
        "Slurm bin directory (contains sacct, squeue, scontrol)" \
        "${DETECTED_SLURM_BIN:-/cm/shared/apps/slurm/current/bin}"

    if [ -d "$SLURM_BIN_DIR" ]; then
        ok "Found Slurm bin dir: $SLURM_BIN_DIR"
        for bin in sacct squeue scontrol; do
            if [ -x "$SLURM_BIN_DIR/$bin" ]; then
                ok "  $bin — OK"
            else
                warn "  $bin — NOT FOUND in $SLURM_BIN_DIR"
            fi
        done
    else
        warn "Slurm bin dir not found: $SLURM_BIN_DIR"
        warn "The agent will fall back to PATH resolution. Set SLURM_BIN_DIR in run_agent.sh."
    fi

    # --- Slurm conf dir ---
    DEFAULT_SLURM_CONF="/cm/shared/apps/slurm/current/etc"
    prompt SLURM_CONF_DIR \
        "Slurm conf directory (contains slurm.conf)" \
        "${SLURM_CONF:-${SLURM_CONF_DIR:-$DEFAULT_SLURM_CONF}}"

    if [ -f "$SLURM_CONF_DIR/slurm.conf" ]; then
        ok "Found slurm.conf in $SLURM_CONF_DIR"
    else
        warn "slurm.conf not found at $SLURM_CONF_DIR/slurm.conf"
        warn "This may cause sacct/squeue to fail if SLURM_CONF_FILE is not set."
    fi
fi

# ============================================================
# 5. Environment variables
# ============================================================
section "Environment variables"

# Dashboard URL
prompt DASHBOARD_URL \
    "Dashboard URL (FastAPI server endpoint)" \
    "http://localhost:8000/api/agent/data"

# Cluster name
prompt CLUSTER_NAME \
    "Cluster name for this agent (unique identifier)" \
    "slurm"

# Default GPU model
prompt DEFAULT_GPU_MODEL \
    "Default GPU model fallback (leave blank for none, e.g. 'a30')" \
    ""

# ============================================================
# 6. Write environment file
# ============================================================
section "Writing environment file"

ENV_FILE="$SCRIPT_DIR/.env"
cat > "$ENV_FILE" <<EOF
# Shovly environment configuration
# Generated by setup.sh on $(date '+%Y-%m-%d %H:%M:%S')
# Source this file or export each variable before running agent/server scripts.

# ── Slurm ────────────────────────────────────────────────────────────────────
SLURM_BIN_DIR="${SLURM_BIN_DIR:-/cm/shared/apps/slurm/current/bin}"
SLURM_CONF_DIR="${SLURM_CONF_DIR:-/cm/shared/apps/slurm/current/etc}"

# ── Dashboard server ─────────────────────────────────────────────────────────
# URL agents POST data to
DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8000/api/agent/data}"
# Hostname/IP the uvicorn server binds to (bare-metal mode only)
UVICORN_HOST="0.0.0.0"
UVICORN_PORT="8000"

# ── Agent ────────────────────────────────────────────────────────────────────
CLUSTER_NAME="${CLUSTER_NAME:-slurm}"
# Last-resort GPU model when job GRES and scontrol node map both lack model info.
# Leave blank for CPU-only clusters or clusters where users always specify model.
DEFAULT_GPU_MODEL="${DEFAULT_GPU_MODEL:-}"

# ── Paths ────────────────────────────────────────────────────────────────────
APP_DIR="${APP_DIR}"
DATA_DIR="${DATA_DIR}"
LOG_DIR="${LOG_DIR}"
DB_PATH="${DATA_DIR}/historical.db"
EOF

ok "Written: $ENV_FILE"
info "  Edit this file to change any settings without re-running setup."

# Also patch run_agent.sh defaults in-place so cron picks them up immediately
if [ -f "$SCRIPT_DIR/run_agent.sh" ]; then
    sed -i.bak \
        -e "s|SLURM_BIN_DIR:-[^}]*}|SLURM_BIN_DIR:-${SLURM_BIN_DIR:-/cm/shared/apps/slurm/current/bin}}|" \
        -e "s|DASHBOARD_URL:-[^}]*}|DASHBOARD_URL:-${DASHBOARD_URL:-http://localhost:8000/api/agent/data}}|" \
        -e "s|CLUSTER_NAME:-[^}]*}|CLUSTER_NAME:-${CLUSTER_NAME:-slurm}}|" \
        "$SCRIPT_DIR/run_agent.sh"
    ok "Patched defaults in run_agent.sh"
fi

# ============================================================
# 7. Cron job setup (agent / both)
# ============================================================
if [[ "$MODE" == "agent" || "$MODE" == "both" ]]; then
    section "Cron job"

    CRON_CMD="*/5 * * * * ${SCRIPT_DIR}/run_agent.sh >> ${LOG_DIR}/shovly-agent.log 2>&1"
    CRON_MARKER="# shovly-agent"

    EXISTING_CRON="$(crontab -l 2>/dev/null || true)"

    if echo "$EXISTING_CRON" | grep -q "run_agent.sh"; then
        ok "Cron entry already exists:"
        echo "$EXISTING_CRON" | grep "run_agent.sh" | while IFS= read -r line; do
            printf "    %s\n" "$line"
        done
        if [ "$INTERACTIVE" -eq 1 ]; then
            printf "  Replace it? [y/N]: "
            read -r replace_cron
        else
            replace_cron="n"
        fi
    else
        replace_cron="y"
    fi

    _rc_lower="$(printf '%s' "${replace_cron}" | tr '[:upper:]' '[:lower:]')"
    if [[ "$_rc_lower" == "y" ]]; then
        # Remove any old shovly entry, append new one
        _cron_installed=0
        printf "%s\n%s  %s\n" "$NEW_CRON" "$CRON_MARKER" "$CRON_CMD" | crontab - && _cron_installed=1 || {
            warn "crontab install failed — add it manually:"
        }
        if [ "$_cron_installed" -eq 1 ]; then
            ok "Cron entry installed: runs every 5 minutes"
        else
            skip "Cron entry NOT installed — add manually:"
        fi
        printf "    ${CYAN}%s${RESET}\n" "$CRON_CMD"
    else
        skip "Cron entry unchanged"
        printf "  To install manually:\n    ${CYAN}%s${RESET}\n" "$CRON_CMD"
    fi
fi

# ============================================================
# 8. Docker Compose check (server / both)
# ============================================================
if [[ "$MODE" == "server" || "$MODE" == "both" ]]; then
    section "Docker (optional — for containerized dashboard)"

    if command -v docker &>/dev/null; then
        DOCKER_VERSION="$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',')"
        ok "Docker found: $DOCKER_VERSION"
        if command -v docker compose &>/dev/null || command -v docker-compose &>/dev/null; then
            ok "Docker Compose available"
        else
            warn "Docker Compose not found — needed for 'docker compose up'"
        fi
    else
        skip "Docker not found — skip if running bare-metal with uvicorn"
    fi
fi

# ============================================================
# 9. Write activation helper
# ============================================================
section "Activation helper"

ACTIVATE_SCRIPT="$SCRIPT_DIR/activate_env.sh"
cat > "$ACTIVATE_SCRIPT" <<'ACTIVATE'
#!/bin/bash
# activate_env.sh — Source this file to load Shovly environment variables into
# the current shell session before running scripts manually.
#
#   source activate_env.sh
ACTIVATE

cat >> "$ACTIVATE_SCRIPT" <<EOF
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
if [ -f "\$SCRIPT_DIR/.env" ]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "\$SCRIPT_DIR/.env"
    set +o allexport
    echo "Shovly environment loaded from \$SCRIPT_DIR/.env"
else
    echo "ERROR: \$SCRIPT_DIR/.env not found. Run setup.sh first."
fi
EOF

chmod +x "$ACTIVATE_SCRIPT"
ok "Written: $ACTIVATE_SCRIPT"

# ============================================================
# 10. Final summary & next-steps checklist
# ============================================================
printf "\n"
printf "${BOLD}${GREEN}=================================================================${RESET}\n"
printf "${BOLD}  Setup complete!${RESET}\n"
printf "${BOLD}${GREEN}=================================================================${RESET}\n"

# Collect any warnings about missing tools so we can tailor the todo list
MISSING_SACCT=0
MISSING_DOCKER=0
[ ! -x "${SLURM_BIN_DIR:-}/sacct" ] && command -v sacct &>/dev/null || MISSING_SACCT=1
command -v docker &>/dev/null || MISSING_DOCKER=1

printf "\n${BOLD}Next steps — run in order:${RESET}\n\n"

STEP=1

# ── Always: review .env ──────────────────────────────────────────────────────
printf "${BOLD}%d. Review and edit the environment file${RESET}\n" "$STEP"; ((STEP++))
printf "   ${CYAN}%s${RESET}\n" "\$EDITOR ${ENV_FILE}"
printf "   Key settings to verify:\n"
printf "     SLURM_BIN_DIR  = %s\n" "${SLURM_BIN_DIR:-<not set>}"
printf "     DASHBOARD_URL  = %s\n" "${DASHBOARD_URL:-<not set>}"
printf "     CLUSTER_NAME   = %s\n" "${CLUSTER_NAME:-<not set>}"
[ -n "$DEFAULT_GPU_MODEL" ] && \
printf "     DEFAULT_GPU_MODEL = %s\n" "$DEFAULT_GPU_MODEL"
printf "\n"

# ── Server mode ──────────────────────────────────────────────────────────────
if [[ "$MODE" == "server" || "$MODE" == "both" ]]; then

    printf "${BOLD}%d. Start the dashboard server${RESET}\n" "$STEP"; ((STEP++))
    if [ "$MISSING_DOCKER" -eq 0 ]; then
        printf "   Option A — Docker (recommended):\n"
        printf "   ${CYAN}cd %s && docker compose up -d${RESET}\n" "$SCRIPT_DIR"
        printf "\n   Option B — Bare-metal (uvicorn directly):\n"
        printf "   ${CYAN}source %s/activate_env.sh${RESET}\n" "$SCRIPT_DIR"
        printf "   ${CYAN}cd %s && venv/bin/uvicorn app:app --host \$UVICORN_HOST --port \$UVICORN_PORT${RESET}\n" "$SCRIPT_DIR"
    else
        printf "   ${CYAN}source %s/activate_env.sh${RESET}\n" "$SCRIPT_DIR"
        printf "   ${CYAN}cd %s && venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000${RESET}\n" "$SCRIPT_DIR"
    fi
    printf "   Dashboard will be at: ${CYAN}http://$(hostname -s):8000/${RESET}\n\n"

fi

# ── Agent mode ───────────────────────────────────────────────────────────────
if [[ "$MODE" == "agent" || "$MODE" == "both" ]]; then

    printf "${BOLD}%d. Verify the agent runs without errors${RESET}\n" "$STEP"; ((STEP++))
    printf "   ${CYAN}source %s/activate_env.sh${RESET}\n" "$SCRIPT_DIR"
    printf "   ${CYAN}SLURM_BIN_DIR=%s \\\\\n     %s/run_agent.sh${RESET}\n" \
        "${SLURM_BIN_DIR:-/cm/shared/apps/slurm/current/bin}" "$SCRIPT_DIR"
    printf "   Check for HTTP 200 in the output. If you see 'Connection refused',\n"
    printf "   the dashboard server is not yet reachable at %s\n\n" "${DASHBOARD_URL:-<URL>}"

    printf "${BOLD}%d. Verify the cron entry${RESET}\n" "$STEP"; ((STEP++))
    printf "   ${CYAN}crontab -l | grep run_agent${RESET}\n\n"

    printf "${BOLD}%d. Tail the agent log to confirm it fires${RESET}\n" "$STEP"; ((STEP++))
    printf "   ${CYAN}tail -f %s/shovly-agent.log${RESET}\n\n" "${LOG_DIR}"

fi

# ── Historical import (both) ─────────────────────────────────────────────────
printf "${BOLD}%d. (Optional) Import historical Slurm data${RESET}\n" "$STEP"; ((STEP++))
printf "   a) Dump sacct history (replace YYYY-MM-DD with your earliest useful date):\n"
printf "   ${CYAN}   SLURM_BIN_DIR=%s \\\\\n     %s/dump_history.sh \\\\\n     --starttime YYYY-MM-DD \\\\\n     --cluster-name %s \\\\\n     --output %s/sacct_raw.csv${RESET}\n\n" \
    "${SLURM_BIN_DIR:-/cm/shared/apps/slurm/current/bin}" \
    "$SCRIPT_DIR" \
    "${CLUSTER_NAME:-slurm}" \
    "${DATA_DIR}"
printf "   b) Import into the SQLite database:\n"
printf "   ${CYAN}   %s/venv/bin/python3 %s/import_history.py \\\\\n     %s/sacct_raw.csv \\\\\n     --node-gpu-map %s/node_gpu_map.csv \\\\\n     --db %s/historical.db${RESET}\n\n" \
    "$SCRIPT_DIR" "$SCRIPT_DIR" \
    "${DATA_DIR}" "${DATA_DIR}" "${DATA_DIR}"

[ -n "$DEFAULT_GPU_MODEL" ] && \
printf "   (Add ${CYAN}--default-gpu-model %s${RESET} if needed for jobs without explicit GPU model)\n\n" \
    "$DEFAULT_GPU_MODEL"

# ── Multi-cluster ─────────────────────────────────────────────────────────────
printf "${BOLD}%d. (Optional) Add more clusters${RESET}\n" "$STEP"; ((STEP++))
printf "   Copy run_agent.sh and .env to each additional login node, then:\n"
printf "   ${CYAN}   CLUSTER_NAME=<new-cluster-name> DASHBOARD_URL=%s \\\\\n     /path/to/run_agent.sh${RESET}\n\n" \
    "${DASHBOARD_URL:-http://<server>:8000/api/agent/data}"

printf "${BOLD}${GREEN}=================================================================${RESET}\n"
printf "  Configuration files written:\n"
printf "    ${CYAN}%s${RESET}   (environment variables)\n" "$ENV_FILE"
printf "    ${CYAN}%s${RESET}  (shell activation helper)\n" "$ACTIVATE_SCRIPT"
printf "    Directories: %s\n" "$DATA_DIR, $LOG_DIR"
printf "${BOLD}${GREEN}=================================================================${RESET}\n\n"
