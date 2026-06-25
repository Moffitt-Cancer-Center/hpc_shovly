#!/bin/bash

# This script generates a CSV inventory of all nodes available to pdsh.
# It gathers hostname, core count, RAM, GPU count, and GPU model.

# Define the output file
OUTPUT_DIR="data"
OUTPUT_FILE="$OUTPUT_DIR/cluster_inventory.csv"

# Ensure the output directory exists
mkdir -p "$OUTPUT_DIR"

# Write the CSV header
echo "Hostname,Cores,RAM_GB,GPU_Count,GPU_Model,CPU_Model" > "$OUTPUT_FILE"

# Use pdsh to execute a command on all nodes and append to the CSV
# The command string is complex, so it's broken down here:
# 1. Get short hostname
# 2. Get core count
# 3. Get total RAM in GB
# 4. Get GPU count (or 0 if nvidia-smi fails)
# 5. Get GPU model (or "N/A" if no GPU)
# 6. Get CPU model name and wrap it in quotes to handle commas/spaces
CMD="hostname -s | tr -d '\\n' && echo -n ',' && \\
     nproc | tr -d '\\n' && echo -n ',' && \\
     free -g | awk '/^Mem:/{print \$2}' | tr -d '\\n' && echo -n ',' && \\
     (nvidia-smi --query-gpu=count --format=csv,noheader | head -n 1 || echo '0') | tr -d '\\n' && echo -n ',' && \\
     (nvidia-smi --query-gpu=gpu_name --format=csv,noheader | head -n 1 || echo 'N/A') | tr -d '\\n' && echo -n ',' && \\
     echo -n '\"' && lscpu | grep 'Model name' | sed -e 's/Model name:[ ]*//' | tr -d '\\n' && echo '\"'"

# Execute the command on all nodes defined in WCOLL
# The output of pdsh will be in the format: hostname: value
# We use sed to remove the "hostname: " prefix
pdsh -w ^all_nodes "$CMD" | sed 's/^[a-zA-Z0-9.-]*: //' >> "$OUTPUT_FILE"

echo "Inventory created at $OUTPUT_FILE"