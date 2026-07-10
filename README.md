# Shovly — HPC Cloud Cost Comparator

Real-time and historical cloud cost analysis for Slurm HPC workloads.

Shovly runs alongside your Slurm cluster and continuously answers the question:
**"What would today's workloads cost if we ran them on AWS or Azure?"**

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Repository Layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Quick Start (Docker)](#quick-start-docker--recommended)
- [Bare-Metal Setup](#bare-metal-setup)
- [Agent Installation](#agent-installation)
- [Loading Historical Data](#loading-historical-data)
- [Updating Cloud Price Lists](#updating-cloud-price-lists)
- [On-Premises Cost Calculator](#on-premises-cost-calculator)
- [Dashboard Features](#dashboard-features)
- [Operational Tools](#operational-tools)
- [Configuration Reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

---

## Overview

Shovly has two components that work together:

1. **Dashboard server** — a FastAPI web application (served via Docker) that receives job data, stores it in SQLite, and serves the dashboard UI.
2. **Slurm agent** — a lightweight Python script installed on each HPC login node that runs every 5 minutes via cron, queries Slurm (`squeue` / `scontrol`), and POSTs the running job list to the dashboard.

The dashboard matches each Slurm job to the cheapest cloud instance that satisfies its CPU, memory, and GPU requirements, then projects cost to the job's time limit. Historical job records are stored permanently so administrators can run date-range reports across years of accounting data.

---

## Architecture

```
  HPC cluster login node                 Dashboard server
  ──────────────────────                 ────────────────
  cron → run_agent.sh
           └─ agent.py
                ├─ squeue (running jobs) ──POST /api/agent/data──▶ app.py
                └─ sacct  (completed)                               │
                                                                    ├─ SQLite (data/historical.db)
  (one-time historical import)                                      └─ static/ (web UI)
  dump_history.sh → sacct_raw.csv
  import_history.py ──────────────────────────────────────────────▶ SQLite
```

Multiple clusters can report to a single dashboard. Each agent identifies itself with `CLUSTER_NAME`.

---

## Repository Layout

```
shovly/
├── app.py                    # FastAPI dashboard server
├── agent.py                  # Slurm job-collection agent
├── run_agent.sh              # Cron wrapper for agent.py
├── import_history.py         # One-time historical data importer
├── dump_history.sh           # sacct CSV export helper
├── validate.py               # Database QA and reporting tool
├── get_hardware.sh           # Node hardware inventory helper
├── setup.sh                  # First-time environment setup
├── requirements.txt          # Server Python dependencies
├── Dockerfile                # Container image definition
├── docker-compose.yml        # Compose service definition
├── .env.example              # Environment variable reference
├── hpc-cost-comparator/
│   ├── load_pricelist.py     # AWS + Azure catalog loader
│   ├── logs/                 # Agent log directory (contents gitignored)
│   └── data/
│       ├── AWS-pricelist.csv
│       └── Azure-pricelist.csv
└── static/
    ├── index.html            # Dashboard HTML
    ├── app.js                # Dashboard JavaScript
    ├── styles.css            # Dashboard CSS
    └── chart.min.js          # Chart.js (bundled — no CDN dependency)
```

Runtime directories created automatically:

| Path | Contents |
|---|---|
| `data/` | SQLite historical database (`historical.db`) |
| `data/sacct_raw.csv` | Raw sacct export (temporary, safe to delete after import) |

---

## Prerequisites

### Dashboard server
- Docker 20.10+ and Docker Compose v2+

### Slurm agent (per HPC login node)
- Python 3.6+ with `pip`
- `squeue` and `scontrol` accessible on the login node
- Network access to the dashboard server (HTTP on the configured port)

---

## Quick Start (Docker — Recommended)

### 1. Clone and configure

```bash
git clone https://github.com/Moffitt-Cancer-Center/hpc_shovly.git
cd hpc_shovly
```

### 2. Run first-time setup

`setup.sh` validates dependencies, creates a Python venv, and writes `.env`:

```bash
bash setup.sh
# For non-interactive (CI/CD) use:
bash setup.sh --mode server --non-interactive
```

### 3. Start the server

```bash
docker compose up -d
```

The dashboard is available at `http://<server>:8000`.

> The dashboard will show no data until at least one agent has reported in.
> Pre-populate historical data with `import_history.py` (see below).

---

## Bare-Metal Setup

If you prefer to run the server without Docker:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

The server expects `data/` (for SQLite) and `static/` to exist relative to its working directory.

---

## Agent Installation

Install the agent on each Slurm login node you want to monitor.

### 1. Deploy the repository

```bash
git clone https://github.com/Moffitt-Cancer-Center/hpc_shovly.git /share/hpc_shared/shovly
```

Or use `rsync` if the login nodes cannot reach GitHub directly.

### 2. Create the agent venv

The agent only needs `requests`; no need to install the full server stack.

```bash
cd /share/hpc_shared/shovly
python3 -m venv venv
venv/bin/pip install requests
```

### 3. Configure `run_agent.sh`

Edit the configuration block at the top of `run_agent.sh`, or export variables before the cron command:

| Variable | Description | Default |
|---|---|---|
| `SLURM_BIN_DIR` | Full path to Slurm `bin/` directory | `/cm/shared/apps/slurm/current/bin` |
| `DASHBOARD_URL` | Agent endpoint URL | `http://localhost:8000/api/agent/data` |
| `CLUSTER_NAME` | Display name for this cluster | `slurm` |
| `DEFAULT_GPU_MODEL` | Fallback GPU model (leave blank if not needed) | _(empty)_ |

### 4. Add to crontab

```cron
*/5 * * * * /share/hpc_shared/shovly/run_agent.sh >> /var/log/shovly-agent.log 2>&1
```

The agent sends two payloads per run:
- **Running jobs** (from `squeue`) — used for the real-time display
- **Recently completed jobs** (from `sacct` since last checkpoint) — appended to the historical database

---

## Loading Historical Data

To pre-populate the dashboard with years of Slurm accounting history:

### Step 1 — Export from Slurm (`dump_history.sh`)

Run on the HPC login node where `sacct` is available:

```bash
./dump_history.sh \
    --starttime 2021-01-01 \
    --cluster-name mycluster \
    --output data/sacct_raw.csv
```

`dump_history.sh` queries `sacct` month-by-month to avoid Slurm DB timeouts on large date ranges.
The output is a pipe-delimited CSV with no header:

```
JobID|User|Submit|Start|End|ReqCPUS|ReqMem|ReqTRES|ElapsedRaw|TimelimitRaw|State|ClusterName
```

### Step 2 — Import (`import_history.py`)

```bash
python3 import_history.py data/sacct_raw.csv --db data/historical.db
```

- Each job is matched to the cheapest cloud instance meeting its resource requirements.
- Import is idempotent — re-running will not create duplicates (primary key: `job_id + cluster`).
- Large imports are batched (10 000 rows at a time) and show progress.

---

## Updating Cloud Price Lists

Replace the CSV files in `hpc-cost-comparator/data/` and restart the server. No code changes are required.

### AWS price list format

| Column | Description |
|---|---|
| `InstanceType` | e.g. `m6i.xlarge` |
| `ProcessorVCPUCount` | vCPU count |
| `MemorySizeInMB` | Memory in **GB** _(column name is misleading)_ |
| `PricePerHour` | On-demand Linux hourly rate (USD) |
| `GPUCount` | Number of GPUs (0 for CPU-only instances) |
| `GPUName` | GPU model string (normalized via `GPU_MODEL_MAP`) |

### Azure price list format

| Column | Description |
|---|---|
| `name` | VM size, e.g. `Standard_D4s_v5` |
| `numberOfCores` | vCPU count |
| `memoryInMB` | Memory in **GB** _(column name is misleading)_ |
| `linuxPrice` | Pay-as-you-go Linux hourly rate (USD) |
| `gpUs` | GPU count |
| `gpuType` | GPU model string |

> **GPU model matching:** GPU type strings from Slurm GRES, AWS, and Azure are all normalized through `GPU_MODEL_MAP` in `hpc-cost-comparator/load_pricelist.py`. If a new GPU model is not recognized, add it to that map.

### Instance matching priority (GPU jobs)

For jobs that request GPUs, the matching algorithm applies these passes in order, always selecting the cheapest qualifying instance:

| Pass | GPU count | GPU model | vCPUs | Memory |
|---|---|---|---|---|
| 1 | ≥ requested | exact match | ≥ requested | ≥ requested |
| 2 | ≥ requested | exact match | ≥ requested | relaxed |
| 3 | ≥ requested | any model | ≥ requested | — |
| 4 | ≥ requested | — | — | — |
| fallback | CPU-only instance (catalog data issue) | | | |

---

## On-Premises Cost Calculator

The dashboard includes an optional per-cluster on-premises cost calculator (collapsible section at the bottom of the page). It allows you to benchmark cloud costs against your actual infrastructure investment.

For each cluster, enter:

| Field | Description |
|---|---|
| **CapEx ($)** | Total hardware purchase cost |
| **Lifecycle (yrs)** | Amortization period (1–50 years) |
| **OpEx ($)** | Ongoing operating expenditure |
| **OpEx Period** | Annual or monthly |
| **Funding Source** | Federal Grant, Philanthropy, Capital Fund, Departmental, or Other |
| **Note** | Free-text funding attribution |

The calculator derives:
- **Annual Total** = (CapEx ÷ lifecycle) + annualized OpEx
- **Cost/hr** = Annual Total ÷ 8,760

When a date range is selected in the Historical Cost Calculator, the on-prem section contributes:
- **On-Prem Total** — prorated cost for the selected period
- **On-Prem Eff. $/hr** — total prorated cost ÷ actual compute hours (apples-to-apples vs. cloud per-hour rates)
- A third line on the daily cost chart

Values are persisted to the SQLite database and survive server restarts. Use **Clear** to remove a cluster's entry.

---

## Dashboard Features

| Section | Description |
|---|---|
| **Active Jobs** | Real-time table of all running Slurm jobs, matched to cloud instances with projected cost |
| **Cost Comparison** | Bar chart comparing total projected AWS vs. Azure cost for all live jobs |
| **Instance Distribution** | Doughnut chart of the AWS instance types in use |
| **Historical Cost Calculator** | Date-range and per-cluster totals (jobs, AWS, Azure, compute hours) with a daily trend line chart |
| **Top 50 Users** | Ranked by compute hours; per-user AWS and Azure totals for the selected period |
| **On-Premises Calculator** | Optional CapEx/OpEx entry for on-prem vs. cloud benchmarking |

The dashboard auto-refreshes every 5 seconds. All tables support click-to-sort on any column.

---

## Operational Tools

### `validate.py` — Database QA and reporting

```bash
# Full report (all subcommands)
venv/bin/python3 validate.py all

# Individual subcommands
venv/bin/python3 validate.py summary          # database overview and totals
venv/bin/python3 validate.py anomalies        # zero-cost or suspicious records
venv/bin/python3 validate.py top-users        # top users by compute hours
venv/bin/python3 validate.py cluster-stats    # per-cluster breakdown

# Filters (apply to any subcommand)
venv/bin/python3 validate.py summary --cluster gpu-cluster
venv/bin/python3 validate.py summary --start 2024-01-01 --end 2024-12-31
```

### `get_hardware.sh` — Node hardware inventory

Outputs a single CSV row of hardware specs for the node it runs on:

```
hostname,cpu_cores,ram_gb,gpu_count,gpu_model,cpu_model
```

Run via `pdsh` or `clush` across all nodes to build a full cluster hardware inventory:

```bash
pdsh -w node[001-200] /share/hpc_shared/shovly/get_hardware.sh > hardware_inventory.csv
```

---

## Configuration Reference

Copy `.env.example` to `.env` and fill in values for your site. `setup.sh` generates `.env` automatically during interactive setup.

| Variable | Component | Description |
|---|---|---|
| `SLURM_BIN_DIR` | Agent | Full path to Slurm `bin/` (e.g. `/cm/shared/apps/slurm/current/bin`) |
| `SLURM_CONF_DIR` | Agent | Full path to Slurm `etc/` |
| `DASHBOARD_URL` | Agent | URL agents POST to (e.g. `http://10.0.0.5:8000/api/agent/data`) |
| `UVICORN_HOST` | Server | Bind address for bare-metal mode (`0.0.0.0` = all interfaces) |
| `UVICORN_PORT` | Server | Port for bare-metal mode (default `8000`) |
| `CLUSTER_NAME` | Agent | Display name for this cluster in the dashboard |
| `DEFAULT_GPU_MODEL` | Agent | Fallback GPU model when jobs don't specify one (leave blank for CPU-only clusters) |
| `DB_PATH` | Both | Absolute path to the SQLite historical database |

---

## Troubleshooting

### `database is locked` when running `validate.py`

The dashboard server keeps the SQLite connection open. WAL mode is enabled automatically on first start, which allows concurrent readers. If the error persists after a server restart, check that no other process has the database open exclusively.

### `ModuleNotFoundError: Cannot import load_pricelist`

`hpc-cost-comparator/` is missing from the deployment. Pull the latest code and rebuild:

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

### Agent not sending data

1. Confirm the cron job is installed: `crontab -l`
2. Check the cron log: `tail -50 /var/log/shovly-agent.log`
3. Test server reachability from the login node: `curl -s http://<server>:8000/api/clusters`
4. Verify Slurm is accessible: `squeue --version`

### Jobs matched to unexpected cloud instances

Review the matching logic in `app.py → find_best_instance()`. GPU jobs are matched first by GPU count and model; CPU/memory jobs match the cheapest instance with sufficient vCPUs and memory. If a GPU model isn't recognized, add it to `GPU_MODEL_MAP` in `hpc-cost-comparator/load_pricelist.py`.

### Historical costs differ from expectations

Cloud instance prices change over time. Historical jobs are always priced at the **current** rates in the price-list CSVs — this correctly answers "what would those jobs cost today?" rather than "what would they have cost at the time?" Update the price-list CSVs and re-run `import_history.py` to re-price all records.

---

## Contributing

Pull requests are welcome. Please open an issue first to discuss significant changes.

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit with a descriptive message
4. Open a pull request against `main`

When updating instance matching logic (`find_best_instance`) or the price-list loader (`load_pricelist.py`), run `validate.py anomalies` against the historical database afterward to verify no regressions.

---

*Developed and maintained by the Research Computing team at [Moffitt Cancer Center](https://moffitt.org).*
