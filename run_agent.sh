#!/bin/bash
# run_agent.sh — Shovly agent cron wrapper
#
# Designed to be called directly from cron with no environment setup required.
# Uses a local virtualenv (venv/) so there is no conda/module dependency.
#
# One-time setup on the cluster:
#   cd /share/hpc_shared/shovly
#   python3 -m venv venv
#   venv/bin/pip install requests
#
# Crontab entry:
#   */5 * * * * /share/hpc_shared/shovly/run_agent.sh >> /var/log/shovly-agent.log 2>&1
#
# All settings below can be overridden by exporting the variable before the
# cron command, e.g.:
#   */5 * * * * CLUSTER_NAME=gpu-cluster /share/hpc_shared/shovly/run_agent.sh ...

# --------------------------------------------------------------------------
# Configuration  (override via environment variables)
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SLURM_BIN_DIR="${SLURM_BIN_DIR:-/cm/shared/apps/slurm/current/bin}"
DASHBOARD_URL="${DASHBOARD_URL:-http://red2.moffitt.org:8000/api/agent/data}"
CLUSTER_NAME="${CLUSTER_NAME:-slurm}"
# Set to the GPU model installed in this cluster's nodes (e.g. a30, a100, v100).
# Only used as a fallback when a job requested GPUs but didn't specify the model type
# (i.e. submitted with --gres=gpu:N rather than --gres=gpu:a30:N).
# Leave blank for CPU-only clusters or clusters where all jobs specify the model.
DEFAULT_GPU_MODEL="${DEFAULT_GPU_MODEL:-}"

# --------------------------------------------------------------------------
# Locate Python — prefer the local venv, fall back to system python3
# --------------------------------------------------------------------------
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"

if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: venv not found at $VENV_PYTHON" >&2
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Run:  python3 -m venv $SCRIPT_DIR/venv && $SCRIPT_DIR/venv/bin/pip install requests" >&2
    # Fall back to whatever python3 is in PATH
    PYTHON="$(command -v python3)"
    if [ -z "$PYTHON" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: python3 not found in PATH. Aborting." >&2
        exit 1
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Falling back to $PYTHON" >&2
fi

# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
export SLURM_BIN_DIR

exec "$PYTHON" "$SCRIPT_DIR/agent.py" \
    "$DASHBOARD_URL" \
    --cluster-name="$CLUSTER_NAME" \
    ${DEFAULT_GPU_MODEL:+--default-gpu-model="$DEFAULT_GPU_MODEL"}
