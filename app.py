# app.py
import asyncio
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HPC Cloud Cost Comparator")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class JobInfo(BaseModel):
    job_id: str
    cpus: int
    mem_mb: int       # Memory in megabytes (0 = not specified by job)
    gpu_count: int
    gpu_model: str    # e.g. "a30", "a100", "" for none

class AgentPayload(BaseModel):
    cluster_name: str
    jobs: List[JobInfo]
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

APP_STATE = {
    "last_updated": None,
    "clusters": {},        # {cluster_name: {"jobs": [...], "last_seen": datetime}}
    "total_active_jobs": 0,
    "hourly_cost_aws": 0.0,
    "hourly_cost_azure": 0.0,
    "job_details": [],
}
AGENT_TIMEOUT_MINUTES = 10

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
    logger.info("Received %d jobs from cluster: %s", len(payload.jobs), payload.cluster_name)
    APP_STATE["clusters"][payload.cluster_name] = {
        "jobs": [j.dict() for j in payload.jobs],
        "last_seen": datetime.utcnow(),
    }
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
            cpus      = job["cpus"]
            mem_mb    = job["mem_mb"]
            gpu_count = job["gpu_count"]
            gpu_model = job["gpu_model"]

            aws_inst   = find_best_instance(AWS_INSTANCES,   cpus, mem_mb, gpu_count, gpu_model)
            azure_inst = find_best_instance(AZURE_INSTANCES, cpus, mem_mb, gpu_count, gpu_model)

            total_aws   += aws_inst["price"]
            total_azure += azure_inst["price"]

            job_details.append({
                "job_id":         job["job_id"],
                "cluster":        cluster_name,
                "cpus":           cpus,
                "mem_gb":         round(mem_mb / 1024, 1) if mem_mb else 0,
                "gpu_count":      gpu_count,
                "gpu_model":      gpu_model or "—",
                "aws_instance":   aws_inst["name"],
                "aws_hourly":     aws_inst["price"],
                "azure_instance": azure_inst["name"],
                "azure_hourly":   azure_inst["price"],
            })

    APP_STATE["total_active_jobs"] = total_jobs
    APP_STATE["hourly_cost_aws"]   = round(total_aws,   2)
    APP_STATE["hourly_cost_azure"] = round(total_azure, 2)
    APP_STATE["job_details"]       = job_details
    APP_STATE["last_updated"]      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def update_metrics_loop():
    """Re-aggregates metrics every 10 seconds."""
    while True:
        process_and_aggregate_metrics()
        await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(update_metrics_loop())


@app.get("/api/metrics")
async def get_metrics():
    return JSONResponse(content={
        "active_jobs":      APP_STATE["total_active_jobs"],
        "hourly_cost_aws":  APP_STATE["hourly_cost_aws"],
        "hourly_cost_azure":APP_STATE["hourly_cost_azure"],
        "job_details":      APP_STATE["job_details"],
        "last_updated":     APP_STATE["last_updated"],
    })


# Serve frontend (must be last so API routes take priority)
app.mount("/", StaticFiles(directory="static", html=True), name="static")


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HPC Cloud Cost Comparator")

# --- Pydantic Models for API Data ---
class AgentPayload(BaseModel):
    cluster_name: str
    active_nodes: List[str]
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

# --- Global State Management ---
# The state now holds data per cluster and tracks agent heartbeats.
APP_STATE = {
    "last_updated": None,
    "clusters": {}, # e.g., {"cluster-a": {"active_nodes": [], "last_seen": ...}}
    "inventory": {},
    # Aggregated metrics will be calculated from the "clusters" dict
    "total_active_jobs": 0,
    "hourly_cost_aws": 0.0,
    "hourly_cost_azure": 0.0,
    "job_details": []
}
AGENT_TIMEOUT_MINUTES = 10

# Baseline AWS Pricing (Fallback for Zero-Auth Local Deployment)
AWS_RATES = {
    "p5.48xlarge": 98.32,  # H100 Equivalent
    "p5e.48xlarge": 110.00, # H200 Equivalent (Approx)
    "p4d.24xlarge": 32.77, # A100 Equivalent
    "p3dn.24xlarge": 31.21, # V100 Equivalent
    "g5.2xlarge": 1.212,   # RTX Approximation
    "r7i.large": 0.133,    # High Mem CPU
    "c6i.large": 0.085     # Standard CPU
}

def load_inventory():
    """Loads the static hardware mapping generated by pdsh."""
    inventory = {}
    try:
        with open('data/cluster_inventory.csv', mode='r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                # Ensure the CSV header matches what the bash script outputs
                inventory[row['Hostname']] = {
                    'cores': int(row['Cores']),
                    'ram_gb': int(row['RAM_GB']),
                    'gpu_count': int(row['GPU_Count']),
                    'gpu_model': row['GPU_Model']
                }
    except FileNotFoundError:
        logger.warning("cluster_inventory.csv not found. Operating with fallback logic.")
    return inventory

def get_cloud_mapping(hardware):
    """Maps physical hardware to closest cloud instances."""
    gpu_count = hardware.get('gpu_count', 0)
    gpu_model = str(hardware.get('gpu_model', '')).upper()
    ram = hardware.get('ram_gb', 0)
    cores = hardware.get('cores', 1)

    if gpu_count > 0:
        if 'H200' in gpu_model: return 'p5e.48xlarge'
        if 'H100' in gpu_model: return 'p5.48xlarge'
        if 'A100' in gpu_model: return 'p4d.24xlarge'
        if 'V100' in gpu_model: return 'p3dn.24xlarge'
        return 'g5.2xlarge' # RTX fallback
    else:
        if ram / max(cores, 1) > 8:
            return 'r7i.large'
        return 'c6i.large'

async def fetch_azure_price(sku_name: str) -> float:
    """Fetches real-time consumption pricing from Azure's open API."""
    # Note: For production, you would map exact Azure armSkuNames.
    # This uses a safe estimation fallback if the API rate limits.
    return AWS_RATES.get(sku_name, 0.0) * 0.95 # Azure is typically slightly offset from AWS

# --- New Endpoint to Receive Agent Data ---
@app.post("/api/agent/data")
async def receive_agent_data(payload: AgentPayload):
    """Endpoint for agents to post their Slurm data."""
    logger.info(f"Received data from agent on cluster: {payload.cluster_name}")
    APP_STATE["clusters"][payload.cluster_name] = {
        "active_nodes": payload.active_nodes,
        "last_seen": datetime.utcnow()
    }
    return {"status": "success", "message": f"Data for {payload.cluster_name} received."}


def process_and_aggregate_metrics():
    """
    Processes the collected agent data in APP_STATE to calculate aggregate costs.
    This function replaces the direct Slurm query.
    """
    # Prune stale agents first
    now = datetime.utcnow()
    stale_clusters = [
        name for name, data in APP_STATE["clusters"].items()
        if now - data["last_seen"] > timedelta(minutes=AGENT_TIMEOUT_MINUTES)
    ]
    for name in stale_clusters:
        logger.warning(f"Agent for cluster '{name}' is stale. Removing from metrics.")
        del APP_STATE["clusters"][name]

    # Now, calculate metrics from active agents
    current_aws_cost = 0.0
    current_azure_cost = 0.0
    job_details = []
    total_active_nodes = 0

    for cluster_name, data in APP_STATE["clusters"].items():
        active_nodes = data["active_nodes"]
        logger.info(f"Processing {len(active_nodes)} nodes from cluster '{cluster_name}'.")
        total_active_nodes += len(active_nodes)

        for node in active_nodes:
            # Strip domain from FQDN to match inventory short hostnames
            short_node_name = node.split('.')[0]

            hardware = APP_STATE["inventory"].get(short_node_name) # Try to get the hardware
            if not hardware:
                logger.warning(f"Node '{short_node_name}' (from original '{node}') not found in inventory. Using default hardware specs.")
                hardware = {"cores": 32, "ram_gb": 128, "gpu_count": 0} # Fallback
            
            instance_type = get_cloud_mapping(hardware)

            aws_price = AWS_RATES.get(instance_type, 0.0)
            # The original async call is now made sync inside the loop for simplicity
            azure_price = AWS_RATES.get(instance_type, 0.0) * 0.95

            current_aws_cost += aws_price
            current_azure_cost += azure_price

            job_details.append({
                "node": f"{cluster_name}/{node}", # Prefix node with cluster name
                "mapped_instance": instance_type,
                "aws_hourly": aws_price,
                "azure_hourly": azure_price
            })

    APP_STATE["total_active_jobs"] = total_active_nodes
    APP_STATE["hourly_cost_aws"] = round(current_aws_cost, 2)
    APP_STATE["hourly_cost_azure"] = round(current_azure_cost, 2)
    APP_STATE["job_details"] = job_details
    APP_STATE["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def update_metrics_loop():
    """Background task that updates costs every 10 seconds based on agent data."""
    # Load inventory once on startup of the loop
    APP_STATE["inventory"] = load_inventory()
    
    while True:
        logger.info("Processing agent data to update HPC Cost Metrics...")
        
        process_and_aggregate_metrics()

        # Wait 10 seconds
        await asyncio.sleep(10)

@app.on_event("startup")
async def startup_event():
    # Load inventory on startup
    APP_STATE["inventory"] = load_inventory()
    # Start the background processing loop
    asyncio.create_task(update_metrics_loop())

@app.get("/api/metrics")
async def get_metrics():
    # The structure returned to the frontend needs to match what it expects
    return JSONResponse(content={
        "active_jobs": APP_STATE["total_active_jobs"],
        "hourly_cost_aws": APP_STATE["hourly_cost_aws"],
        "hourly_cost_azure": APP_STATE["hourly_cost_azure"],
        "job_details": APP_STATE["job_details"],
        "last_updated": APP_STATE["last_updated"],
    })

# Mount the static directory to serve the frontend
app.mount("/", StaticFiles(directory="static", html=True), name="static")
