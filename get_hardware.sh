#!/bin/bash
# get_hardware.sh

HOSTNAME=$(hostname -s)
CPU_CORES=$(nproc)
CPU_MODEL=$(lscpu | awk -F: '/^Model name/ {gsub(/^[ \t]+/, "", $2); print $2}')
RAM_GB=$(free -g | awk '/^Mem:/{print $2}')

# Check for NVIDIA GPUs
if command -v nvidia-smi &> /dev/null; then
    GPU_COUNT=$(nvidia-smi -L | wc -l)
    # Grab the name of the first GPU (assuming homogeneous GPUs per individual node)
    GPU_MODEL=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n 1 | sed 's/NVIDIA //g')
else
    GPU_COUNT=0
    GPU_MODEL="None"
fi

# Output as CSV row
echo "$HOSTNAME,$CPU_CORES,$RAM_GB,$GPU_COUNT,$GPU_MODEL,\"$CPU_MODEL\""