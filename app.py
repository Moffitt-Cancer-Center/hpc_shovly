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
# Instance catalogs — loaded from CSV price lists at startup
# ---------------------------------------------------------------------------
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hpc-cost-comparator"))
from load_pricelist import GPU_MODEL_MAP, load_catalogs as _load_catalogs
del _sys

AWS_INSTANCES, AZURE_INSTANCES = _load_catalogs()


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
