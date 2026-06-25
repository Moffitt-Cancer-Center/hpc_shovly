#!/bin/bash
# dump_history.sh — Dump Slurm accounting history to a pipe-delimited CSV file
# for import into the Shovly historical database.
#
# Usage:
#   ./dump_history.sh [--starttime YYYY-MM-DD] [--endtime YYYY-MM-DD] \
#                     [--cluster-name NAME] [--output PATH]
#
# Output: data/sacct_raw.csv  (pipe-delimited, NO header)
# Columns: JobID|User|Submit|Start|End|ReqCPUS|ReqMem|ReqTRES|ElapsedRaw|TimelimitRaw|State|ClusterName
#
# After running this on the cluster, import with:
#   python3 import_history.py [data/sacct_raw.csv] [--db data/historical.db]
#
# Environment:
#   SLURM_BIN_DIR  - Optional path to Slurm binaries (e.g. /cm/shared/apps/slurm/current/bin)

SLURM_BIN_DIR="${SLURM_BIN_DIR:-}"
START_TIME=""
END_TIME=""
CLUSTER_NAME="default"
OUTPUT_DIR="data"
OUTPUT_FILE=""

usage() {
    echo "Usage: $0 [--starttime YYYY-MM-DD] [--endtime YYYY-MM-DD] [--cluster-name NAME] [--output PATH]"
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --starttime)   START_TIME="$2";   shift 2 ;;
        --endtime)     END_TIME="$2";     shift 2 ;;
        --cluster-name) CLUSTER_NAME="$2"; shift 2 ;;
        --output)      OUTPUT_FILE="$2";  shift 2 ;;
        -h|--help)     usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

if [ -n "$SLURM_BIN_DIR" ]; then
    SACCT="${SLURM_BIN_DIR}/sacct"
else
    SACCT="sacct"
fi

if ! command -v "$SACCT" > /dev/null 2>&1 && [ ! -x "$SACCT" ]; then
    echo "ERROR: sacct not found at '${SACCT}'. Set SLURM_BIN_DIR to the Slurm bin directory."
    exit 1
fi

if [ -z "$OUTPUT_FILE" ]; then
    OUTPUT_FILE="${OUTPUT_DIR}/sacct_raw.csv"
fi

mkdir -p "$(dirname "$OUTPUT_FILE")"

echo "========================================"
echo "  Shovly Historical Dump"
echo "========================================"
echo "  Cluster:    $CLUSTER_NAME"
echo "  Start time: ${START_TIME:-'(beginning of accounting)'}"
echo "  End time:   ${END_TIME:-'(now)'}"
echo "  Output:     $OUTPUT_FILE"
echo "========================================"

SACCT_ARGS="-a -P --noheader"
SACCT_ARGS="$SACCT_ARGS --format=JobID,User,Submit,Start,End,ReqCPUS,ReqMem,ReqTRES,ElapsedRaw,TimelimitRaw,State"

if [ -n "$START_TIME" ]; then
    SACCT_ARGS="$SACCT_ARGS --starttime=${START_TIME}"
fi
if [ -n "$END_TIME" ]; then
    SACCT_ARGS="$SACCT_ARGS --endtime=${END_TIME}"
fi

# Run sacct, filter out job-step records (lines where JobID contains '.'),
# and append the cluster name as the final field.
BEFORE=$(wc -l < "$OUTPUT_FILE" 2>/dev/null || echo 0)

$SACCT $SACCT_ARGS \
    | awk -F'|' -v cluster="$CLUSTER_NAME" \
        'NF==11 && $1 !~ /\./ && $4 != "Unknown" && $4 != "" { print $0 "|" cluster }' \
    >> "$OUTPUT_FILE"

AFTER=$(wc -l < "$OUTPUT_FILE" 2>/dev/null || echo 0)
NEW_RECORDS=$(( AFTER - BEFORE ))

echo "Done. Appended ${NEW_RECORDS} job records."
echo "File now contains ${AFTER} total records: ${OUTPUT_FILE}"
echo ""
echo "Next step — import into Shovly:"
echo "  python3 import_history.py ${OUTPUT_FILE} --db data/historical.db"
