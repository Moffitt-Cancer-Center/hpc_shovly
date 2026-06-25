# agent.py
import os
import subprocess
import logging
import argparse
from datetime import datetime

try:
    import requests
except ImportError:
    raise SystemExit("The 'requests' package is required. Run: pip install requests")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def get_cmd(slurm_bin_dir, cmd):
    """Returns the full path to a Slurm binary."""
    if slurm_bin_dir:
        return os.path.join(slurm_bin_dir, cmd)
    return cmd


def parse_mem_mb(mem_str):
    """
    Converts a Slurm memory string to megabytes.
    Handles: '0', '16384', '16384M', '16G', '1T', '512K', 'N/A', 'UNLIMITED'
    """
    s = mem_str.strip().upper()
    if not s or s in ('N/A', '0', 'UNLIMITED'):
        return 0
    try:
        if s.endswith('T'):
            return int(float(s[:-1]) * 1024 * 1024)
        if s.endswith('G'):
            return int(float(s[:-1]) * 1024)
        if s.endswith('M'):
            return int(float(s[:-1]))
        if s.endswith('K'):
            return max(1, int(float(s[:-1]) / 1024))
        return int(s)  # bare number is MB per Slurm convention
    except ValueError:
        return 0


def parse_gres(gres_str):
    """
    Parses a Slurm GRES string into (gpu_count, gpu_model).
    Examples:
      'gpu:a30:2'        -> (2, 'a30')
      'gpu:1'            -> (1, '')
      'N/A'              -> (0, '')
      'gpu:a30:2,cpu:4'  -> (2, 'a30')  [first gpu entry wins]
    """
    if not gres_str:
        return 0, ''
    for entry in gres_str.strip().lower().split(','):
        entry = entry.strip()
        if not entry.startswith('gpu'):
            continue
        parts = entry.split(':')
        if len(parts) == 3:
            try:
                return int(parts[2]), parts[1]
            except ValueError:
                continue
        elif len(parts) == 2:
            try:
                return int(parts[1]), ''
            except ValueError:
                return 1, parts[1]
    return 0, ''


def get_running_jobs(slurm_bin_dir=None):
    """
    Queries Slurm for all RUNNING jobs.
    Returns a list of dicts: job_id, cpus, mem_mb, gpu_count, gpu_model.
    Uses squeue format: job_id|cpus|min_memory|gres
    """
    squeue = get_cmd(slurm_bin_dir, 'squeue')
    try:
        result = subprocess.run(
            [squeue, '-h', '-t', 'RUNNING', '-o', '%i|%C|%m|%b'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        output = result.stdout.decode('utf-8').strip()
        if not output:
            logger.info("No running jobs found.")
            return []

        jobs = []
        for line in output.splitlines():
            parts = line.strip().split('|')
            if len(parts) != 4:
                continue
            job_id, cpus_str, mem_str, gres_str = parts
            try:
                cpus = int(cpus_str.strip())
            except ValueError:
                cpus = 1
            mem_mb = parse_mem_mb(mem_str)
            gpu_count, gpu_model = parse_gres(gres_str)
            jobs.append({
                "job_id": job_id.strip(),
                "cpus": cpus,
                "mem_mb": mem_mb,
                "gpu_count": gpu_count,
                "gpu_model": gpu_model,
            })
        return jobs

    except FileNotFoundError:
        logger.error("squeue not found at '%s'. Set SLURM_BIN_DIR to the correct path.", squeue)
        return []
    except subprocess.CalledProcessError as e:
        logger.error("squeue failed: %s", e.stderr.decode('utf-8'))
        return []


def main():
    parser = argparse.ArgumentParser(description="Shovly per-job Slurm data collection agent.")
    parser.add_argument(
        "dashboard_url",
        help="URL of the dashboard agent endpoint (e.g., http://red2.moffitt.org:8000/api/agent/data)"
    )
    parser.add_argument(
        "--cluster-name", required=True,
        help="Unique name for this cluster (e.g., moffitt-hpc-1)"
    )
    args = parser.parse_args()

    slurm_bin_dir = os.environ.get('SLURM_BIN_DIR')
    if slurm_bin_dir:
        logger.info("Using custom Slurm binary path: %s", slurm_bin_dir)

    jobs = get_running_jobs(slurm_bin_dir)
    logger.info("Found %d running jobs. Sending to %s...", len(jobs), args.dashboard_url)

    payload = {
        "cluster_name": args.cluster_name,
        "jobs": jobs,
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        response = requests.post(args.dashboard_url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info("Success: %s", response.json())
    except requests.RequestException as e:
        logger.error("Failed to send data: %s", e)


if __name__ == "__main__":
    main()
