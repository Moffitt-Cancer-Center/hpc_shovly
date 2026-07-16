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
DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8000/api/agent/data}"
CLUSTER_NAME="${CLUSTER_NAME:-slurm}"
# Set to the GPU model installed in this cluster's nodes (e.g. a30, a100, v100).
# Only used as a fallback when a job requested GPUs but didn't specify the model type
# (i.e. submitted with --gres=gpu:N rather than --gres=gpu:a30:N).
# Leave blank for CPU-only clusters or clusters where all jobs specify the model.
DEFAULT_GPU_MODEL="${DEFAULT_GPU_MODEL:-}"

# --------------------------------------------------------------------------
# SLURM_CONF — critical for cron: without it Slurm attempts DNS SRV discovery
# which fails in cron's minimal environment (no search domains).
#
# Override in crontab:
#   */5 * * * * SLURM_CONF=/etc/slurm/slurm.conf /share/hpc_shared/shovly/run_agent.sh
#
# Or set it here permanently:
#   SLURM_CONF="/etc/slurm/slurm.conf"
# --------------------------------------------------------------------------
if [ -z "${SLURM_CONF:-}" ]; then
    for _try_conf in \
        "${SLURM_BIN_DIR%/bin}/etc/slurm.conf" \
        "/etc/slurm/slurm.conf" \
        "/etc/slurm-llnl/slurm.conf" \
        "/cm/shared/apps/slurm/current/etc/slurm.conf"; do
        if [ -f "$_try_conf" ]; then
            export SLURM_CONF="$_try_conf"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] INFO: Auto-detected SLURM_CONF=$SLURM_CONF" >&2
            break
        fi
    done
    if [ -z "${SLURM_CONF:-}" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: SLURM_CONF not found in common locations." >&2
        echo "  Set SLURM_CONF=/path/to/slurm.conf in your crontab to fix DNS SRV failures." >&2
    fi
fi

# SSL / HTTPS options — needed when the dashboard is behind a reverse proxy
# (e.g., Open OnDemand) that uses an internal or self-signed certificate.
#
# SSL_NO_VERIFY=true  — skip certificate verification entirely.
#   WARNING: Only use on trusted internal networks.
#   Example: SSL_NO_VERIFY=true /share/hpc_shared/shovly/run_agent.sh
SSL_NO_VERIFY="${SSL_NO_VERIFY:-false}"
#
# SSL_CA_BUNDLE=/path/to/bundle.pem  — trust a specific CA certificate bundle.
#   Preferred over SSL_NO_VERIFY. Point this at your institution's CA bundle.
#   Common paths:
#     RHEL/CentOS/Rocky: /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
#     Debian/Ubuntu:     /etc/ssl/certs/ca-certificates.crt
SSL_CA_BUNDLE="${SSL_CA_BUNDLE:-}"

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
    ${DEFAULT_GPU_MODEL:+--default-gpu-model="$DEFAULT_GPU_MODEL"} \
    $([ "${SSL_NO_VERIFY:-false}" = "true" ] && echo "--ssl-no-verify") \
    ${SSL_CA_BUNDLE:+--ssl-ca-bundle="$SSL_CA_BUNDLE"}
