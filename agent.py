# agent.py
import os
import subprocess
import requests
import logging
import argparse
import json
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_slurm_bin_path(slurm_bin_dir):
    """Constructs the full path for a Slurm command."""
    if slurm_bin_dir:
        return lambda cmd: os.path.join(slurm_bin_dir, cmd)
    return lambda cmd: cmd # Assume it's in PATH

def get_active_slurm_nodes(slurm_bin_dir=None):
    """
    Queries Slurm for running jobs, expands compressed node ranges,
    and returns a unique set of individual active hostnames.
    """
    get_cmd_path = get_slurm_bin_path(slurm_bin_dir)
    squeue_cmd = get_cmd_path('squeue')
    scontrol_cmd = get_cmd_path('scontrol')

    try:
        result = subprocess.run(
            [squeue_cmd, '-h', '-t', 'RUNNING', '-o', '%N'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        )
        raw_nodelists = result.stdout.strip()
        if not raw_nodelists:
            return []

        expanded = subprocess.run(
            [scontrol_cmd, 'show', 'hostnames', raw_nodelists],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        )
        
        unique_nodes = list(set(line.strip() for line in expanded.stdout.splitlines() if line.strip()))
        return unique_nodes

    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        logger.error(f"Slurm command failed: {e}")
        if isinstance(e, subprocess.CalledProcessError):
            logger.error(f"Slurm command stdout: {e.stdout}")
            logger.error(f"Slurm command stderr: {e.stderr}")
        return []

def main():
    parser = argparse.ArgumentParser(description="Shovly data collection agent for Slurm.")
    parser.add_argument("dashboard_url", help="The full URL of the central dashboard's API endpoint (e.g., http://your-dashboard:8000/api/agent/data)")
    parser.add_argument("--cluster-name", required=True, help="A unique name for the cluster this agent is running on.")
    args = parser.parse_args()

    slurm_bin_dir = os.environ.get('SLURM_BIN_DIR')
    if slurm_bin_dir:
        logger.info(f"Using custom Slurm binary path: {slurm_bin_dir}")

    active_nodes = get_active_slurm_nodes(slurm_bin_dir)
    
    payload = {
        "cluster_name": args.cluster_name,
        "active_nodes": active_nodes,
        "timestamp": datetime.utcnow().isoformat()
    }

    logger.info(f"Found {len(active_nodes)} active nodes. Sending to dashboard at {args.dashboard_url}...")

    try:
        response = requests.post(args.dashboard_url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Data successfully sent to the dashboard.")
        logger.info(f"Response: {response.json()}")
    except requests.RequestException as e:
        logger.error(f"Failed to send data to the dashboard: {e}")

if __name__ == "__main__":
    main()
