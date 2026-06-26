# app.py
import asyncio
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HPC Cloud Cost Comparator")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class JobInfo(BaseModel):
    job_id: str
    cpus: int
    mem_mb: int                 # Memory in megabytes (0 = not specified by job)
    gpu_count: int
    gpu_model: str              # e.g. "a30", "a100", "" for none
    time_limit_minutes: int = 0 # 0 = UNLIMITED or not reported by agent

class CompletedJob(BaseModel):
    job_id: str
    cluster: str
    username: str
    start_time: int = 0
    end_time: int = 0
    req_cpus: int = 1
    req_mem_mb: int = 0
    gpu_count: int = 0
    gpu_model: str = ""
    time_limit_min: int = 0
    elapsed_min: int = 0
    state: str = ""

class AgentPayload(BaseModel):
    cluster_name: str
    jobs: List[JobInfo]
    completed_jobs: List[CompletedJob] = []
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

APP_STATE = {
    "last_updated": None,
    "clusters": {},        # {cluster_name: {"jobs": [...], "last_seen": datetime}}
    "total_active_jobs": 0,
    "projected_cost_aws": 0.0,
    "projected_cost_azure": 0.0,
    "job_details": [],
}
AGENT_TIMEOUT_MINUTES = 10

# ---------------------------------------------------------------------------
# SQLite historical database
# ---------------------------------------------------------------------------
DB_PATH = "data/historical.db"
_db_conn: Optional[sqlite3.Connection] = None
_db_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    """Returns the shared SQLite connection, creating it if necessary."""
    global _db_conn
    if _db_conn is None:
        os.makedirs("data", exist_ok=True)
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return _db_conn


def init_db() -> None:
    """Creates the historical jobs table and indexes if they don't exist."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id         TEXT,
            cluster        TEXT,
            username       TEXT    NOT NULL,
            start_time     INTEGER,
            end_time       INTEGER,
            req_cpus       INTEGER DEFAULT 1,
            req_mem_mb     INTEGER DEFAULT 0,
            gpu_count      INTEGER DEFAULT 0,
            gpu_model      TEXT    DEFAULT '',
            time_limit_min INTEGER DEFAULT 0,
            elapsed_min    INTEGER DEFAULT 0,
            state          TEXT    DEFAULT '',
            aws_instance   TEXT    DEFAULT '',
            aws_total      REAL    DEFAULT 0.0,
            azure_instance TEXT    DEFAULT '',
            azure_total    REAL    DEFAULT 0.0,
            PRIMARY KEY (job_id, cluster)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_start   ON jobs(start_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user    ON jobs(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster ON jobs(cluster)")
    conn.commit()
    logger.info("Historical database ready at %s", DB_PATH)

# ---------------------------------------------------------------------------
# GPU model normalisation
# Maps on-prem GRES names to canonical cloud GPU model names.
# A30 -> A10G: both are Ampere-class 24 GB cards; A10G is the closest
# available instance type in AWS/Azure.
# ---------------------------------------------------------------------------

GPU_MODEL_MAP = {
    "h100": "H100", "h200": "H200",
    "a100": "A100",
    "a30":  "A10G", "a10g": "A10G", "a10": "A10G",
    "v100": "V100",
    "t4":   "T4",
    "l40":  "L40S", "l40s": "L40S",
}

# ---------------------------------------------------------------------------
# AWS instance catalog  (on-demand, us-east-1, June 2026 approximate)
# Fields: name, vcpus, mem_gb, gpu_count, gpu_model, price ($/hr)
# ---------------------------------------------------------------------------

AWS_INSTANCES = [
    # H100
    {"name": "p5.48xlarge",    "vcpus": 192, "mem_gb": 2048, "gpu_count": 8, "gpu_model": "H100",  "price": 98.32},
    # A100
    {"name": "p4d.24xlarge",   "vcpus": 96,  "mem_gb": 1152, "gpu_count": 8, "gpu_model": "A100",  "price": 32.77},
    {"name": "p4de.24xlarge",  "vcpus": 96,  "mem_gb": 1152, "gpu_count": 8, "gpu_model": "A100",  "price": 40.96},
    # V100
    {"name": "p3.2xlarge",     "vcpus": 8,   "mem_gb": 61,   "gpu_count": 1, "gpu_model": "V100",  "price": 3.06},
    {"name": "p3.8xlarge",     "vcpus": 32,  "mem_gb": 244,  "gpu_count": 4, "gpu_model": "V100",  "price": 12.24},
    {"name": "p3.16xlarge",    "vcpus": 64,  "mem_gb": 488,  "gpu_count": 8, "gpu_model": "V100",  "price": 24.48},
    {"name": "p3dn.24xlarge",  "vcpus": 96,  "mem_gb": 768,  "gpu_count": 8, "gpu_model": "V100",  "price": 31.21},
    # A10G  (g5 series)
    {"name": "g5.xlarge",      "vcpus": 4,   "mem_gb": 16,   "gpu_count": 1, "gpu_model": "A10G",  "price": 1.006},
    {"name": "g5.2xlarge",     "vcpus": 8,   "mem_gb": 32,   "gpu_count": 1, "gpu_model": "A10G",  "price": 1.212},
    {"name": "g5.4xlarge",     "vcpus": 16,  "mem_gb": 64,   "gpu_count": 1, "gpu_model": "A10G",  "price": 1.624},
    {"name": "g5.8xlarge",     "vcpus": 32,  "mem_gb": 128,  "gpu_count": 1, "gpu_model": "A10G",  "price": 2.448},
    {"name": "g5.12xlarge",    "vcpus": 48,  "mem_gb": 192,  "gpu_count": 4, "gpu_model": "A10G",  "price": 5.672},
    {"name": "g5.16xlarge",    "vcpus": 64,  "mem_gb": 256,  "gpu_count": 1, "gpu_model": "A10G",  "price": 4.096},
    {"name": "g5.24xlarge",    "vcpus": 96,  "mem_gb": 384,  "gpu_count": 4, "gpu_model": "A10G",  "price": 8.144},
    {"name": "g5.48xlarge",    "vcpus": 192, "mem_gb": 768,  "gpu_count": 8, "gpu_model": "A10G",  "price": 16.288},
    # T4  (g4dn series)
    {"name": "g4dn.xlarge",    "vcpus": 4,   "mem_gb": 16,   "gpu_count": 1, "gpu_model": "T4",    "price": 0.526},
    {"name": "g4dn.2xlarge",   "vcpus": 8,   "mem_gb": 32,   "gpu_count": 1, "gpu_model": "T4",    "price": 0.752},
    {"name": "g4dn.4xlarge",   "vcpus": 16,  "mem_gb": 64,   "gpu_count": 1, "gpu_model": "T4",    "price": 1.204},
    {"name": "g4dn.8xlarge",   "vcpus": 32,  "mem_gb": 128,  "gpu_count": 1, "gpu_model": "T4",    "price": 2.264},
    {"name": "g4dn.12xlarge",  "vcpus": 48,  "mem_gb": 192,  "gpu_count": 4, "gpu_model": "T4",    "price": 3.912},
    {"name": "g4dn.16xlarge",  "vcpus": 64,  "mem_gb": 256,  "gpu_count": 1, "gpu_model": "T4",    "price": 4.528},
    # Compute optimised  (c6i)
    {"name": "c6i.large",      "vcpus": 2,   "mem_gb": 4,    "gpu_count": 0, "gpu_model": "",      "price": 0.085},
    {"name": "c6i.xlarge",     "vcpus": 4,   "mem_gb": 8,    "gpu_count": 0, "gpu_model": "",      "price": 0.170},
    {"name": "c6i.2xlarge",    "vcpus": 8,   "mem_gb": 16,   "gpu_count": 0, "gpu_model": "",      "price": 0.340},
    {"name": "c6i.4xlarge",    "vcpus": 16,  "mem_gb": 32,   "gpu_count": 0, "gpu_model": "",      "price": 0.680},
    {"name": "c6i.8xlarge",    "vcpus": 32,  "mem_gb": 64,   "gpu_count": 0, "gpu_model": "",      "price": 1.360},
    {"name": "c6i.12xlarge",   "vcpus": 48,  "mem_gb": 96,   "gpu_count": 0, "gpu_model": "",      "price": 2.040},
    {"name": "c6i.16xlarge",   "vcpus": 64,  "mem_gb": 128,  "gpu_count": 0, "gpu_model": "",      "price": 2.720},
    {"name": "c6i.24xlarge",   "vcpus": 96,  "mem_gb": 192,  "gpu_count": 0, "gpu_model": "",      "price": 4.080},
    {"name": "c6i.32xlarge",   "vcpus": 128, "mem_gb": 256,  "gpu_count": 0, "gpu_model": "",      "price": 5.440},
    # General purpose  (m6i)
    {"name": "m6i.large",      "vcpus": 2,   "mem_gb": 8,    "gpu_count": 0, "gpu_model": "",      "price": 0.096},
    {"name": "m6i.xlarge",     "vcpus": 4,   "mem_gb": 16,   "gpu_count": 0, "gpu_model": "",      "price": 0.192},
    {"name": "m6i.2xlarge",    "vcpus": 8,   "mem_gb": 32,   "gpu_count": 0, "gpu_model": "",      "price": 0.384},
    {"name": "m6i.4xlarge",    "vcpus": 16,  "mem_gb": 64,   "gpu_count": 0, "gpu_model": "",      "price": 0.768},
    {"name": "m6i.8xlarge",    "vcpus": 32,  "mem_gb": 128,  "gpu_count": 0, "gpu_model": "",      "price": 1.536},
    {"name": "m6i.12xlarge",   "vcpus": 48,  "mem_gb": 192,  "gpu_count": 0, "gpu_model": "",      "price": 2.304},
    {"name": "m6i.16xlarge",   "vcpus": 64,  "mem_gb": 256,  "gpu_count": 0, "gpu_model": "",      "price": 3.072},
    {"name": "m6i.24xlarge",   "vcpus": 96,  "mem_gb": 384,  "gpu_count": 0, "gpu_model": "",      "price": 4.608},
    {"name": "m6i.32xlarge",   "vcpus": 128, "mem_gb": 512,  "gpu_count": 0, "gpu_model": "",      "price": 6.144},
    # Memory optimised  (r7i)
    {"name": "r7i.large",      "vcpus": 2,   "mem_gb": 16,   "gpu_count": 0, "gpu_model": "",      "price": 0.133},
    {"name": "r7i.xlarge",     "vcpus": 4,   "mem_gb": 32,   "gpu_count": 0, "gpu_model": "",      "price": 0.266},
    {"name": "r7i.2xlarge",    "vcpus": 8,   "mem_gb": 64,   "gpu_count": 0, "gpu_model": "",      "price": 0.532},
    {"name": "r7i.4xlarge",    "vcpus": 16,  "mem_gb": 128,  "gpu_count": 0, "gpu_model": "",      "price": 1.064},
    {"name": "r7i.8xlarge",    "vcpus": 32,  "mem_gb": 256,  "gpu_count": 0, "gpu_model": "",      "price": 2.128},
    {"name": "r7i.12xlarge",   "vcpus": 48,  "mem_gb": 384,  "gpu_count": 0, "gpu_model": "",      "price": 3.192},
    {"name": "r7i.16xlarge",   "vcpus": 64,  "mem_gb": 512,  "gpu_count": 0, "gpu_model": "",      "price": 4.256},
    {"name": "r7i.24xlarge",   "vcpus": 96,  "mem_gb": 768,  "gpu_count": 0, "gpu_model": "",      "price": 6.384},
    {"name": "r7i.48xlarge",   "vcpus": 192, "mem_gb": 1536, "gpu_count": 0, "gpu_model": "",      "price": 12.768},
]

# ---------------------------------------------------------------------------
# Azure instance catalog  (pay-as-you-go, East US, June 2026 approximate)
# ---------------------------------------------------------------------------

AZURE_INSTANCES = [
    # H100  (ND H100 v5)
    {"name": "Standard_ND96isr_H100_v5",  "vcpus": 96,  "mem_gb": 900,  "gpu_count": 8, "gpu_model": "H100",  "price": 98.32},
    # A100  (NC A100 v4 / ND A100 v4)
    {"name": "Standard_NC24ads_A100_v4",  "vcpus": 24,  "mem_gb": 220,  "gpu_count": 1, "gpu_model": "A100",  "price": 3.67},
    {"name": "Standard_NC48ads_A100_v4",  "vcpus": 48,  "mem_gb": 440,  "gpu_count": 2, "gpu_model": "A100",  "price": 7.35},
    {"name": "Standard_NC96ads_A100_v4",  "vcpus": 96,  "mem_gb": 880,  "gpu_count": 4, "gpu_model": "A100",  "price": 14.69},
    {"name": "Standard_ND96asr_v4",       "vcpus": 96,  "mem_gb": 900,  "gpu_count": 8, "gpu_model": "A100",  "price": 32.77},
    # V100  (NCv3)
    {"name": "Standard_NC6s_v3",          "vcpus": 6,   "mem_gb": 112,  "gpu_count": 1, "gpu_model": "V100",  "price": 3.06},
    {"name": "Standard_NC12s_v3",         "vcpus": 12,  "mem_gb": 224,  "gpu_count": 2, "gpu_model": "V100",  "price": 6.12},
    {"name": "Standard_NC24s_v3",         "vcpus": 24,  "mem_gb": 448,  "gpu_count": 4, "gpu_model": "V100",  "price": 12.24},
    # A10G  (NVadsA10_v5)
    {"name": "Standard_NV6ads_A10_v5",    "vcpus": 6,   "mem_gb": 55,   "gpu_count": 1, "gpu_model": "A10G",  "price": 0.908},
    {"name": "Standard_NV12ads_A10_v5",   "vcpus": 12,  "mem_gb": 110,  "gpu_count": 1, "gpu_model": "A10G",  "price": 1.816},
    {"name": "Standard_NV18ads_A10_v5",   "vcpus": 18,  "mem_gb": 220,  "gpu_count": 1, "gpu_model": "A10G",  "price": 2.724},
    {"name": "Standard_NV36ads_A10_v5",   "vcpus": 36,  "mem_gb": 440,  "gpu_count": 2, "gpu_model": "A10G",  "price": 5.448},
    {"name": "Standard_NV72ads_A10_v5",   "vcpus": 72,  "mem_gb": 880,  "gpu_count": 4, "gpu_model": "A10G",  "price": 10.896},
    # T4  (NCasT4_v3)
    {"name": "Standard_NC4as_T4_v3",      "vcpus": 4,   "mem_gb": 28,   "gpu_count": 1, "gpu_model": "T4",    "price": 0.526},
    {"name": "Standard_NC8as_T4_v3",      "vcpus": 8,   "mem_gb": 56,   "gpu_count": 1, "gpu_model": "T4",    "price": 0.752},
    {"name": "Standard_NC16as_T4_v3",     "vcpus": 16,  "mem_gb": 110,  "gpu_count": 1, "gpu_model": "T4",    "price": 1.204},
    {"name": "Standard_NC64as_T4_v3",     "vcpus": 64,  "mem_gb": 440,  "gpu_count": 4, "gpu_model": "T4",    "price": 4.352},
    # Compute optimised  (Fsv2)
    {"name": "Standard_F2s_v2",           "vcpus": 2,   "mem_gb": 4,    "gpu_count": 0, "gpu_model": "",      "price": 0.085},
    {"name": "Standard_F4s_v2",           "vcpus": 4,   "mem_gb": 8,    "gpu_count": 0, "gpu_model": "",      "price": 0.170},
    {"name": "Standard_F8s_v2",           "vcpus": 8,   "mem_gb": 16,   "gpu_count": 0, "gpu_model": "",      "price": 0.338},
    {"name": "Standard_F16s_v2",          "vcpus": 16,  "mem_gb": 32,   "gpu_count": 0, "gpu_model": "",      "price": 0.676},
    {"name": "Standard_F32s_v2",          "vcpus": 32,  "mem_gb": 64,   "gpu_count": 0, "gpu_model": "",      "price": 1.352},
    {"name": "Standard_F48s_v2",          "vcpus": 48,  "mem_gb": 96,   "gpu_count": 0, "gpu_model": "",      "price": 2.028},
    {"name": "Standard_F64s_v2",          "vcpus": 64,  "mem_gb": 128,  "gpu_count": 0, "gpu_model": "",      "price": 2.704},
    {"name": "Standard_F72s_v2",          "vcpus": 72,  "mem_gb": 144,  "gpu_count": 0, "gpu_model": "",      "price": 3.045},
    # General purpose  (Dv5)
    {"name": "Standard_D2s_v5",           "vcpus": 2,   "mem_gb": 8,    "gpu_count": 0, "gpu_model": "",      "price": 0.096},
    {"name": "Standard_D4s_v5",           "vcpus": 4,   "mem_gb": 16,   "gpu_count": 0, "gpu_model": "",      "price": 0.192},
    {"name": "Standard_D8s_v5",           "vcpus": 8,   "mem_gb": 32,   "gpu_count": 0, "gpu_model": "",      "price": 0.384},
    {"name": "Standard_D16s_v5",          "vcpus": 16,  "mem_gb": 64,   "gpu_count": 0, "gpu_model": "",      "price": 0.768},
    {"name": "Standard_D32s_v5",          "vcpus": 32,  "mem_gb": 128,  "gpu_count": 0, "gpu_model": "",      "price": 1.536},
    {"name": "Standard_D48s_v5",          "vcpus": 48,  "mem_gb": 192,  "gpu_count": 0, "gpu_model": "",      "price": 2.304},
    {"name": "Standard_D64s_v5",          "vcpus": 64,  "mem_gb": 256,  "gpu_count": 0, "gpu_model": "",      "price": 3.072},
    {"name": "Standard_D96s_v5",          "vcpus": 96,  "mem_gb": 384,  "gpu_count": 0, "gpu_model": "",      "price": 4.608},
    # Memory optimised  (Ev5)
    {"name": "Standard_E2s_v5",           "vcpus": 2,   "mem_gb": 16,   "gpu_count": 0, "gpu_model": "",      "price": 0.127},
    {"name": "Standard_E4s_v5",           "vcpus": 4,   "mem_gb": 32,   "gpu_count": 0, "gpu_model": "",      "price": 0.254},
    {"name": "Standard_E8s_v5",           "vcpus": 8,   "mem_gb": 64,   "gpu_count": 0, "gpu_model": "",      "price": 0.504},
    {"name": "Standard_E16s_v5",          "vcpus": 16,  "mem_gb": 128,  "gpu_count": 0, "gpu_model": "",      "price": 1.008},
    {"name": "Standard_E32s_v5",          "vcpus": 32,  "mem_gb": 256,  "gpu_count": 0, "gpu_model": "",      "price": 2.016},
    {"name": "Standard_E48s_v5",          "vcpus": 48,  "mem_gb": 384,  "gpu_count": 0, "gpu_model": "",      "price": 3.024},
    {"name": "Standard_E64s_v5",          "vcpus": 64,  "mem_gb": 512,  "gpu_count": 0, "gpu_model": "",      "price": 4.032},
    {"name": "Standard_E96s_v5",          "vcpus": 96,  "mem_gb": 672,  "gpu_count": 0, "gpu_model": "",      "price": 6.048},
]

# ---------------------------------------------------------------------------
# Instance matching
# ---------------------------------------------------------------------------

def find_best_instance(catalog, cpus, mem_mb, gpu_count, gpu_model):
    """
    Returns the cheapest instance in `catalog` that satisfies the job's
    resource requirements.  GPU model is matched first; then count; then
    vCPUs; then memory.  Falls back gracefully when no exact match exists.
    """
    mem_gb = mem_mb / 1024.0
    norm_gpu = GPU_MODEL_MAP.get(gpu_model.lower(), gpu_model.upper()) if gpu_model else ""

    if gpu_count > 0:
        # Pass 1: exact GPU model + count + vCPUs + memory
        c = [i for i in catalog
             if i["gpu_count"] >= gpu_count
             and (not norm_gpu or i["gpu_model"] == norm_gpu)
             and i["vcpus"] >= max(cpus, 1)
             and i["mem_gb"] >= mem_gb]
        # Pass 2: relax memory requirement
        if not c:
            c = [i for i in catalog
                 if i["gpu_count"] >= gpu_count
                 and (not norm_gpu or i["gpu_model"] == norm_gpu)
                 and i["vcpus"] >= max(cpus, 1)]
        # Pass 3: relax GPU model (any GPU model will do)
        if not c:
            c = [i for i in catalog
                 if i["gpu_count"] >= gpu_count
                 and i["vcpus"] >= max(cpus, 1)]
        # Pass 4: just GPU count
        if not c:
            c = [i for i in catalog if i["gpu_count"] >= gpu_count]
        if c:
            return min(c, key=lambda x: x["price"])

    # CPU/memory job (or GPU job with nothing matching in the GPU passes)
    c = [i for i in catalog
         if i["gpu_count"] == 0
         and i["vcpus"] >= max(cpus, 1)
         and i["mem_gb"] >= mem_gb]
    if not c:
        c = [i for i in catalog
             if i["gpu_count"] == 0 and i["vcpus"] >= max(cpus, 1)]
    if not c:
        c = [i for i in catalog if i["gpu_count"] == 0]
    if c:
        return min(c, key=lambda x: x["price"])

    return min(catalog, key=lambda x: x["price"])  # absolute fallback

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.post("/api/agent/data")
async def receive_agent_data(payload: AgentPayload):
    """Agents POST per-job Slurm resource data here."""
    logger.info(
        "Received %d running jobs, %d completed jobs from cluster: %s",
        len(payload.jobs), len(payload.completed_jobs), payload.cluster_name
    )

    # Update live cluster state
    APP_STATE["clusters"][payload.cluster_name] = {
        "jobs": [j.dict() for j in payload.jobs],
        "last_seen": datetime.utcnow(),
    }

    # Insert completed jobs into the historical SQLite database
    if payload.completed_jobs:
        rows = []
        for cj in payload.completed_jobs:
            aws_inst   = find_best_instance(AWS_INSTANCES,   cj.req_cpus, cj.req_mem_mb, cj.gpu_count, cj.gpu_model)
            azure_inst = find_best_instance(AZURE_INSTANCES, cj.req_cpus, cj.req_mem_mb, cj.gpu_count, cj.gpu_model)
            time_hours = (cj.time_limit_min or cj.elapsed_min) / 60.0
            rows.append((
                cj.job_id, cj.cluster, cj.username,
                cj.start_time, cj.end_time,
                cj.req_cpus, cj.req_mem_mb,
                cj.gpu_count, cj.gpu_model,
                cj.time_limit_min, cj.elapsed_min,
                cj.state,
                aws_inst["name"],   round(aws_inst["price"]   * time_hours, 4),
                azure_inst["name"], round(azure_inst["price"] * time_hours, 4),
            ))
        try:
            with _db_lock:
                db = get_db()
                db.executemany(
                    "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows
                )
                db.commit()
            logger.info("Appended %d completed jobs to historical DB.", len(rows))
        except Exception as exc:
            logger.error("Failed to insert completed jobs: %s", exc)

    return {"status": "success", "message": f"Data for {payload.cluster_name} received."}


def process_and_aggregate_metrics():
    """Computes per-job cloud equivalent costs and aggregates the totals."""
    now = datetime.utcnow()

    # Remove stale agents
    stale = [name for name, d in APP_STATE["clusters"].items()
             if now - d["last_seen"] > timedelta(minutes=AGENT_TIMEOUT_MINUTES)]
    for name in stale:
        logger.warning("Agent for cluster '%s' is stale. Removing.", name)
        del APP_STATE["clusters"][name]

    total_jobs = 0
    total_aws = 0.0
    total_azure = 0.0
    job_details = []

    for cluster_name, data in APP_STATE["clusters"].items():
        jobs = data["jobs"]
        total_jobs += len(jobs)
        logger.info("Processing %d jobs from cluster '%s'.", len(jobs), cluster_name)

        for job in jobs:
            cpus               = job["cpus"]
            mem_mb             = job["mem_mb"]
            gpu_count          = job["gpu_count"]
            gpu_model          = job["gpu_model"]
            time_limit_minutes = job.get("time_limit_minutes", 0)

            aws_inst   = find_best_instance(AWS_INSTANCES,   cpus, mem_mb, gpu_count, gpu_model)
            azure_inst = find_best_instance(AZURE_INSTANCES, cpus, mem_mb, gpu_count, gpu_model)

            # Projected total cost = hourly rate × time limit hours
            # Fall back to 1 h if time limit is 0 (UNLIMITED / not reported)
            time_hours = time_limit_minutes / 60.0 if time_limit_minutes > 0 else 1.0
            aws_total   = round(aws_inst["price"]   * time_hours, 2)
            azure_total = round(azure_inst["price"] * time_hours, 2)

            total_aws   += aws_total
            total_azure += azure_total

            job_details.append({
                "job_id":          job["job_id"],
                "cluster":         cluster_name,
                "cpus":            cpus,
                "mem_gb":          round(mem_mb / 1024, 1) if mem_mb else 0,
                "gpu_count":       gpu_count,
                "gpu_model":       gpu_model or "—",
                "time_limit_min":  time_limit_minutes,
                "aws_instance":    aws_inst["name"],
                "aws_hourly":      aws_inst["price"],
                "aws_total":       aws_total,
                "azure_instance":  azure_inst["name"],
                "azure_hourly":    azure_inst["price"],
                "azure_total":     azure_total,
            })

    APP_STATE["total_active_jobs"]   = total_jobs
    APP_STATE["projected_cost_aws"]  = round(total_aws,   2)
    APP_STATE["projected_cost_azure"]= round(total_azure, 2)
    APP_STATE["job_details"]         = job_details
    APP_STATE["last_updated"]        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def update_metrics_loop():
    """Re-aggregates metrics every 10 seconds."""
    while True:
        process_and_aggregate_metrics()
        await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(update_metrics_loop())


@app.get("/api/metrics")
async def get_metrics():
    return JSONResponse(content={
        "active_jobs":          APP_STATE["total_active_jobs"],
        "projected_cost_aws":   APP_STATE["projected_cost_aws"],
        "projected_cost_azure": APP_STATE["projected_cost_azure"],
        "job_details":          APP_STATE["job_details"],
        "last_updated":         APP_STATE["last_updated"],
    })


@app.get("/api/historical")
async def get_historical(
    start:   Optional[str] = Query(None, description="Start date YYYY-MM-DD (inclusive)"),
    end:     Optional[str] = Query(None, description="End date YYYY-MM-DD (inclusive)"),
    cluster: str           = Query("all", description="Cluster name or 'all'")
):
    """Return daily cost totals for the given date range and cluster."""
    try:
        conditions, params = [], []
        if start:
            conditions.append("start_time >= ?")
            params.append(int(datetime.strptime(start, "%Y-%m-%d").timestamp()))
        if end:
            conditions.append("start_time < ?")
            end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
            params.append(int(end_dt.timestamp()))
        if cluster != "all":
            conditions.append("cluster = ?")
            params.append(cluster)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with _db_lock:
            db = get_db()
            row = db.execute(
                f"SELECT COUNT(*), SUM(aws_total), SUM(azure_total), SUM(elapsed_min) FROM jobs {where}",
                params
            ).fetchone()
            daily_rows = db.execute(
                f"""SELECT date(start_time, 'unixepoch') AS day,
                           SUM(aws_total), SUM(azure_total), COUNT(*)
                    FROM jobs {where}
                    GROUP BY day ORDER BY day""",
                params
            ).fetchall()

        return JSONResponse(content={
            "total_jobs":          row[0] or 0,
            "total_aws":           round(row[1] or 0.0, 2),
            "total_azure":         round(row[2] or 0.0, 2),
            "total_compute_hours": round((row[3] or 0) / 60.0, 1),
            "daily": [
                {"date": r[0], "aws_total": round(r[1] or 0, 2),
                 "azure_total": round(r[2] or 0, 2), "jobs": r[3]}
                for r in daily_rows
            ],
        })
    except ValueError as exc:
        return JSONResponse(content={"error": f"Invalid date format: {exc}"}, status_code=400)
    except Exception as exc:
        logger.error("Historical query failed: %s", exc)
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.get("/api/top-users")
async def get_top_users(
    start:   Optional[str] = Query(None, description="Start date YYYY-MM-DD (inclusive)"),
    end:     Optional[str] = Query(None, description="End date YYYY-MM-DD (inclusive)"),
    cluster: str           = Query("all", description="Cluster name or 'all'")
):
    """Return the top 50 users ranked by total compute hours in the given range."""
    try:
        conditions, params = [], []
        if start:
            conditions.append("start_time >= ?")
            params.append(int(datetime.strptime(start, "%Y-%m-%d").timestamp()))
        if end:
            conditions.append("start_time < ?")
            end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
            params.append(int(end_dt.timestamp()))
        if cluster != "all":
            conditions.append("cluster = ?")
            params.append(cluster)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with _db_lock:
            db = get_db()
            rows = db.execute(
                f"""SELECT username,
                           COUNT(*)                         AS job_count,
                           ROUND(SUM(elapsed_min)/60.0, 1) AS total_hours,
                           ROUND(SUM(aws_total),   2)      AS aws_total,
                           ROUND(SUM(azure_total), 2)      AS azure_total
                    FROM jobs {where}
                    GROUP BY username
                    ORDER BY total_hours DESC
                    LIMIT 50""",
                params
            ).fetchall()

        return JSONResponse(content={"users": [
            {"username": r[0], "job_count": r[1], "total_hours": r[2],
             "aws_total": r[3], "azure_total": r[4]}
            for r in rows
        ]})
    except ValueError as exc:
        return JSONResponse(content={"error": f"Invalid date format: {exc}"}, status_code=400)
    except Exception as exc:
        logger.error("Top-users query failed: %s", exc)
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.get("/api/clusters")
async def get_clusters():
    """Return the list of known cluster names from the historical database."""
    try:
        with _db_lock:
            rows = get_db().execute("SELECT DISTINCT cluster FROM jobs ORDER BY cluster").fetchall()
        names = [r[0] for r in rows]
        # Also include any live clusters not yet in the DB
        for name in APP_STATE["clusters"]:
            if name not in names:
                names.append(name)
        return JSONResponse(content={"clusters": sorted(names)})
    except Exception as exc:
        logger.error("Clusters query failed: %s", exc)
        return JSONResponse(content={"clusters": list(APP_STATE["clusters"].keys())})


# Serve frontend (must be last so API routes take priority)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
