#!/bin/bash
# activate_env.sh — Source this file to load Shovly environment variables into
# the current shell session before running scripts manually.
#
#   source activate_env.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +o allexport
    echo "Shovly environment loaded from $SCRIPT_DIR/.env"
else
    echo "ERROR: $SCRIPT_DIR/.env not found. Run setup.sh first."
fi
