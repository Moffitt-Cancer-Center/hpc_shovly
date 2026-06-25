# agent.py
import os
import subprocess
import logging
import argparse
import time as time_module
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


def parse_sacct_mem_mb(mem_str):
    """
    Parse sacct ReqMem: '<value><unit>[n|c]' where n=per-node, c=per-cpu.
    Strips the trailing n/c suffix before parsing.
    Examples: '32768Mn', '16Gc', '0', 'UNLIMITED'
    """
    s = mem_str.strip().upper()
    if not s or s in ('0', 'UNLIMITED', 'N/A'):
        return 0
    if s.endswith('N') or s.endswith('C'):
        s = s[:-1]
    return parse_mem_mb(s)


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


def parse_tres_gpu(tres_str):
    """
    Parse GPU info from sacct ReqTRES field.
    Examples:
      'billing=1,cpu=8,mem=32G,node=1,gres/gpu=2'  -> (2, '')
      'gres/gpu:a30=1'                               -> (1, 'a30')
    """
    if not tres_str or tres_str.strip() == '':
        return 0, ''
    for part in tres_str.strip().lower().split(','):
        part = part.strip()
        if 'gres/gpu' not in part:
            continue
        if '=' in part:
            key, val = part.rsplit('=', 1)
            try:
                count = int(val)
            except ValueError:
                count = 1
            model = key.split(':', 1)[1] if ':' in key else ''
            return count, model
    return 0, ''


def parse_time_limit_minutes(time_str):
    """
    Parse squeue %l time-limit string to total minutes.
    Handles: D-HH:MM:SS, HH:MM:SS, MM:SS, MM, UNLIMITED, INVALID
    """
    s = time_str.strip().upper()
    if not s or s in ('UNLIMITED', 'INVALID', 'N/A', ''):
        return 0
    try:
        days = 0
        if '-' in s:
            day_part, time_part = s.split('-', 1)
            days = int(day_part)
        else:
            time_part = s
        parts = time_part.split(':')
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, sec = 0, int(parts[0]), int(parts[1])
        elif len(parts) == 1:
            h, m, sec = 0, int(parts[0]), 0
        else:
            return 0
        return days * 1440 + h * 60 + m + (1 if sec >= 30 else 0)
    except (ValueError, IndexError):
        return 0


def parse_sacct_timestamp(ts_str):
    """Convert sacct timestamp string (YYYY-MM-DDTHH:MM:SS) to Unix timestamp."""
    s = ts_str.strip()
    if not s or s.lower() in ('unknown', 'none', ''):
        return 0
    try:
        return int(time_module.mktime(time_module.strptime(s, '%Y-%m-%dT%H:%M:%S')))
    except (ValueError, OverflowError):
        return 0


# ---------------------------------------------------------------------------
# Checkpoint management (tracks last sacct query so we don't re-import jobs)
# ---------------------------------------------------------------------------

def _checkpoint_path(cluster_name):
    return os.path.join('data', '.checkpoint_{}'.format(cluster_name))


def load_checkpoint(cluster_name):
    """Returns Unix timestamp of last successful sacct sync; defaults to 24 h ago."""
    try:
        with open(_checkpoint_path(cluster_name), 'r') as fh:
            return float(fh.read().strip())
    except (IOError, ValueError):
        return time_module.time() - 86400  # 24 hours ago


def save_checkpoint(cluster_name, ts):
    """Persists the timestamp of a successful agent submission."""
    try:
        os.makedirs('data', exist_ok=True)
        with open(_checkpoint_path(cluster_name), 'w') as fh:
            fh.write(str(ts))
    except IOError as e:
        logger.error("Failed to save checkpoint: %s", e)


# ---------------------------------------------------------------------------
# Slurm queries
# ---------------------------------------------------------------------------

def get_running_jobs(slurm_bin_dir=None):
    """
    Queries Slurm for all RUNNING jobs including time limit.
    Returns a list of dicts with: job_id, cpus, mem_mb, gpu_count, gpu_model,
    time_limit_minutes.
    """
    squeue = get_cmd(slurm_bin_dir, 'squeue')
    try:
        result = subprocess.run(
            [squeue, '-h', '-t', 'RUNNING', '-o', '%i|%C|%m|%b|%l'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        output = result.stdout.decode('utf-8').strip()
        if not output:
            logger.info("No running jobs found.")
            return []

        jobs = []
        for line in output.splitlines():
            parts = line.strip().split('|')
            if len(parts) != 5:
                continue
            job_id, cpus_str, mem_str, gres_str, time_str = parts
            try:
                cpus = int(cpus_str.strip())
            except ValueError:
                cpus = 1
            mem_mb = parse_mem_mb(mem_str)
            gpu_count, gpu_model = parse_gres(gres_str)
            time_limit_minutes = parse_time_limit_minutes(time_str)
            jobs.append({
                "job_id": job_id.strip(),
                "cpus": cpus,
                "mem_mb": mem_mb,
                "gpu_count": gpu_count,
                "gpu_model": gpu_model,
                "time_limit_minutes": time_limit_minutes,
            })
        return jobs

    except FileNotFoundError:
        logger.error("squeue not found at '%s'. Set SLURM_BIN_DIR to the correct path.", squeue)
        return []
    except subprocess.CalledProcessError as e:
        logger.error("squeue failed: %s", e.stderr.decode('utf-8'))
        return []


def get_completed_jobs_since(checkpoint_ts, cluster_name, slurm_bin_dir=None):
    """
    Queries sacct for jobs completed since checkpoint_ts (Unix timestamp).
    Returns a list of completed-job dicts for historical storage.
    """
    sacct = get_cmd(slurm_bin_dir, 'sacct')
    start_str = time_module.strftime('%Y-%m-%dT%H:%M:%S', time_module.localtime(checkpoint_ts))

    try:
        result = subprocess.run(
            [sacct, '-a', '-P', '--noheader',
             '--starttime', start_str,
             '--state', 'COMPLETED,FAILED,CANCELLED,TIMEOUT',
             '--format', 'JobID,User,Submit,Start,End,ReqCPUS,ReqMem,ReqTRES,ElapsedRaw,TimelimitRaw,State'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        output = result.stdout.decode('utf-8').strip()
        if not output:
            return []

        jobs = []
        for line in output.splitlines():
            parts = line.strip().split('|')
            if len(parts) != 11:
                continue
            job_id = parts[0].strip()
            if '.' in job_id:
                continue  # Skip job steps (e.g. 12345.batch)

            start_ts = parse_sacct_timestamp(parts[3])
            if start_ts == 0:
                continue  # Skip jobs that never actually started

            try:
                req_cpus = int(parts[5].strip())
            except ValueError:
                req_cpus = 1

            try:
                elapsed_min = int(parts[8].strip()) // 60
            except ValueError:
                elapsed_min = 0

            try:
                time_limit_min = int(parts[9].strip())
            except ValueError:
                time_limit_min = 0

            gpu_count, gpu_model = parse_tres_gpu(parts[7])

            jobs.append({
                'job_id':        job_id,
                'cluster':       cluster_name,
                'username':      parts[1].strip(),
                'start_time':    start_ts,
                'end_time':      parse_sacct_timestamp(parts[4]),
                'req_cpus':      req_cpus,
                'req_mem_mb':    parse_sacct_mem_mb(parts[6]),
                'gpu_count':     gpu_count,
                'gpu_model':     gpu_model,
                'time_limit_min': time_limit_min,
                'elapsed_min':   elapsed_min,
                'state':         parts[10].strip(),
            })
        return jobs

    except FileNotFoundError:
        logger.error("sacct not found at '%s'. Historical tracking unavailable.", sacct)
        return []
    except subprocess.CalledProcessError as e:
        logger.error("sacct failed: %s", e.stderr.decode('utf-8'))
        return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Shovly Slurm data collection agent.")
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

    # --- Running jobs (for the live dashboard) ---
    jobs = get_running_jobs(slurm_bin_dir)
    logger.info("Found %d running jobs.", len(jobs))

    # --- Completed jobs since last checkpoint (for historical DB) ---
    checkpoint_ts = load_checkpoint(args.cluster_name)
    now_ts = time_module.time()
    completed_jobs = get_completed_jobs_since(checkpoint_ts, args.cluster_name, slurm_bin_dir)
    logger.info("Found %d completed jobs since last checkpoint.", len(completed_jobs))

    payload = {
        "cluster_name":   args.cluster_name,
        "jobs":           jobs,
        "completed_jobs": completed_jobs,
        "timestamp":      datetime.utcnow().isoformat(),
    }

    try:
        response = requests.post(args.dashboard_url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info("Success: %s", response.json())
        # Only advance the checkpoint after a successful submission
        save_checkpoint(args.cluster_name, now_ts)
    except requests.RequestException as e:
        logger.error("Failed to send data: %s", e)


if __name__ == "__main__":
    main()
