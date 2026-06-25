#!/bin/bash
# dump_history.sh — Dump Slurm accounting history to a pipe-delimited CSV file
# for import into the Shovly historical database.
#
# Queries sacct month-by-month to avoid Slurm DB timeout on large date ranges.
# Each chunk's errors are shown immediately so silent failures are visible.
#
# Usage:
#   ./dump_history.sh --starttime YYYY-MM-DD [--endtime YYYY-MM-DD] \
#                     [--cluster-name NAME] [--output PATH] [--no-chunk]
#
# Output: data/sacct_raw.csv  (pipe-delimited, NO header, safe to re-run)
# Columns: JobID|User|Submit|Start|End|ReqCPUS|ReqMem|ReqTRES|ElapsedRaw|TimelimitRaw|State|ClusterName
#
# After dumping, import with:
#   python3 import_history.py data/sacct_raw.csv --db data/historical.db
#
# Environment:
#   SLURM_BIN_DIR  - Path to Slurm bin directory (e.g. /cm/shared/apps/slurm/current/bin)

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------
SLURM_BIN_DIR="${SLURM_BIN_DIR:-}"
START_TIME=""
END_TIME=""
CLUSTER_NAME="default"
OUTPUT_FILE=""
NO_CHUNK=0

SACCT_FORMAT="JobID,User,Submit,Start,End,ReqCPUS,ReqMem,ReqTRES,ElapsedRaw,TimelimitRaw,State"

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
usage() {
    echo "Usage: $0 --starttime YYYY-MM-DD [--endtime YYYY-MM-DD] [--cluster-name NAME] [--output PATH] [--no-chunk]"
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --starttime)    START_TIME="$2";    shift 2 ;;
        --endtime)      END_TIME="$2";      shift 2 ;;
        --cluster-name) CLUSTER_NAME="$2";  shift 2 ;;
        --output)       OUTPUT_FILE="$2";   shift 2 ;;
        --no-chunk)     NO_CHUNK=1;         shift 1 ;;
        -h|--help)      usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

if [ -z "$START_TIME" ]; then
    echo "ERROR: --starttime is required (e.g. --starttime 2023-01-01)"
    usage
fi

# --------------------------------------------------------------------------
# Locate sacct
# --------------------------------------------------------------------------
if [ -n "$SLURM_BIN_DIR" ]; then
    SACCT="${SLURM_BIN_DIR}/sacct"
else
    SACCT="$(command -v sacct)"
fi

if [ -z "$SACCT" ] || [ ! -x "$SACCT" ]; then
    echo "ERROR: sacct not found. Set SLURM_BIN_DIR to the Slurm bin directory."
    exit 1
fi

# --------------------------------------------------------------------------
# Output file
# --------------------------------------------------------------------------
if [ -z "$OUTPUT_FILE" ]; then
    OUTPUT_FILE="data/sacct_raw.csv"
fi
mkdir -p "$(dirname "$OUTPUT_FILE")"
touch "$OUTPUT_FILE"   # ensure file exists before any wc -l calls

# Default end date to today
if [ -z "$END_TIME" ]; then
    END_TIME="$(date +%Y-%m-%d)"
fi

echo "========================================"
echo "  Shovly Historical Dump"
echo "========================================"
echo "  sacct:      $SACCT"
echo "  Cluster:    $CLUSTER_NAME"
echo "  Start:      $START_TIME"
echo "  End:        $END_TIME"
echo "  Output:     $OUTPUT_FILE"
echo "  Chunking:   $([ $NO_CHUNK -eq 1 ] && echo 'disabled (--no-chunk)' || echo 'monthly')"
echo "========================================"

TOTAL_BEFORE=$(wc -l < "$OUTPUT_FILE")

# --------------------------------------------------------------------------
# Core function: run one sacct chunk and append results
# --------------------------------------------------------------------------
run_chunk() {
    local chunk_start="$1"
    local chunk_end="$2"

    printf "  [%s] %s → %s ... " "$(date '+%H:%M:%S')" "$chunk_start" "$chunk_end"

    local tmp_out tmp_err
    tmp_out="$(mktemp)"
    tmp_err="$(mktemp)"

    "$SACCT" -a -P --noheader \
        --format="$SACCT_FORMAT" \
        --starttime="$chunk_start" \
        --endtime="$chunk_end" \
        > "$tmp_out" 2> "$tmp_err"
    local exit_code=$?

    # Always show sacct stderr so failures are never silent
    if [ -s "$tmp_err" ]; then
        echo ""
        echo "  [sacct stderr]:"
        sed 's/^/    /' "$tmp_err"
    fi

    if [ $exit_code -ne 0 ]; then
        echo "  WARNING: sacct exited $exit_code for $chunk_start → $chunk_end. Skipping chunk."
        rm -f "$tmp_out" "$tmp_err"
        return 1
    fi

    local raw_lines
    raw_lines=$(wc -l < "$tmp_out")

    awk -F'|' -v cluster="$CLUSTER_NAME" \
        'NF==11 && $1 !~ /\./ && $4 != "Unknown" && $4 != "" { print $0 "|" cluster }' \
        "$tmp_out" >> "$OUTPUT_FILE"

    local filtered
    filtered=$(awk -F'|' 'NF==11 && $1 !~ /\./ && $4 != "Unknown" && $4 != ""' "$tmp_out" | wc -l)
    printf "%d records appended (from %d raw lines)\n" "$filtered" "$raw_lines"

    rm -f "$tmp_out" "$tmp_err"
    return 0
}

# --------------------------------------------------------------------------
# Single chunk (--no-chunk or very small range)
# --------------------------------------------------------------------------
if [ $NO_CHUNK -eq 1 ]; then
    echo "Running single sacct query (no chunking)..."
    run_chunk "$START_TIME" "$END_TIME"
else
    # --------------------------------------------------------------------------
    # Month-by-month chunking
    # --------------------------------------------------------------------------
    # Parse start/end into year and month integers
    start_year=$(date -d "$START_TIME" +%Y)
    start_month=$(date -d "$START_TIME" +%-m)   # %-m = no leading zero
    end_year=$(date -d "$END_TIME" +%Y)
    end_month=$(date -d "$END_TIME" +%-m)

    total_months=$(( (end_year - start_year) * 12 + end_month - start_month + 1 ))
    echo "Running $total_months monthly chunks from $START_TIME to $END_TIME..."
    echo ""

    cur_year=$start_year
    cur_month=$start_month
    chunk_num=0

    while true; do
        chunk_num=$(( chunk_num + 1 ))
        chunk_start=$(printf "%04d-%02d-01" $cur_year $cur_month)

        # Compute first day of next month (used as exclusive upper bound)
        if [ $cur_month -eq 12 ]; then
            next_year=$(( cur_year + 1 ))
            next_month=1
        else
            next_year=$cur_year
            next_month=$(( cur_month + 1 ))
        fi
        chunk_end=$(printf "%04d-%02d-01" $next_year $next_month)

        printf "  Chunk %d/%d " $chunk_num $total_months
        run_chunk "$chunk_start" "$chunk_end"

        # Advance to next month
        cur_year=$next_year
        cur_month=$next_month

        # Stop once we have passed the end month
        if [ $cur_year -gt $end_year ] || \
           ( [ $cur_year -eq $end_year ] && [ $cur_month -gt $end_month ] ); then
            break
        fi
    done
fi

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
TOTAL_AFTER=$(wc -l < "$OUTPUT_FILE")
NEW_RECORDS=$(( TOTAL_AFTER - TOTAL_BEFORE ))

echo ""
echo "========================================"
echo "  Done."
echo "  New records appended: $NEW_RECORDS"
echo "  Total records in file: $TOTAL_AFTER"
echo "  Output: $OUTPUT_FILE"
echo "========================================"
echo ""
echo "Next step — import into Shovly:"
echo "  python3 import_history.py ${OUTPUT_FILE} --db data/historical.db"
