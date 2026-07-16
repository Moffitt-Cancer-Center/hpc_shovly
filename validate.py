#!/usr/bin/env python3
"""
validate.py — Shovly data validation and audit tool.

Queries the SQLite database (and optionally the live API / Slurm) to verify
that the cost calculations, GPU detection, and aggregated values shown on the
dashboard are correct.

Usage:
    python3 validate.py <subcommand> [options]

Subcommands:
    summary        Overall DB health and coverage overview
    users          Per-user cost and compute breakdown
    cluster        Per-cluster stats
    cloud          AWS vs Azure cost comparison and instance distribution
    gpu            GPU job detection, model distribution, cost impact
    cpu            CPU-only job analysis: vCPU/mem distribution, instance mapping, cost/CPU-hour
    pricing        Verify stored instance prices match the built-in catalog
    recalc-check   Recalculate every stored cost and compare vs what is in DB
    live           Compare live dashboard API vs squeue / sacct on the cluster
    anomalies      Flag suspicious records (zero cost GPU jobs, outliers, etc.)
    all            Run all offline checks (summary → anomalies) in sequence

Common options:
    --db PATH          SQLite database (default: data/historical.db)
    --start YYYY-MM-DD Filter by start date (inclusive)
    --end   YYYY-MM-DD Filter by end date   (inclusive)
    --cluster NAME     Limit to one cluster (default: all)
    --top N            Number of rows in ranked tables (default: 20)
    --api URL          Dashboard base URL for live checks (default: http://localhost:8000)
    --slurm-bin DIR    Slurm bin directory for live checks
"""

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

# ── colour helpers ────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()
def _c(code, text): return f"\033[{code}m{text}\033[0m" if _TTY else text
RED    = lambda t: _c("31", t)
GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
CYAN   = lambda t: _c("36", t)
BOLD   = lambda t: _c("1",  t)

def hr(char="─", width=72): print(char * width)
def section(title): print(); hr(); print(BOLD(f"  {title}")); hr()

def fmt_money(v):
    if   v >= 1_000_000: return f"${v/1_000_000:,.1f}M"
    elif v >= 1_000:     return f"${v/1_000:,.1f}K"
    else:                return f"${v:,.2f}"

def fmt_hours(m):
    h = m / 60.0
    if   h >= 8_760: return f"{h/8_760:.1f} yr"
    elif h >= 720:   return f"{h/720:.1f} mo"
    elif h >= 24:    return f"{h/24:.1f} d"
    else:            return f"{h:.1f} h"

def pct(a, b): return f"{100*a/b:.1f}%" if b else "—"

def col_w(rows, headers):
    """Return column widths as max(header, data) for each column."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    return widths

def print_table(headers, rows, color_col=None, warn_col=None):
    if not rows:
        print("  (no data)")
        return
    w = col_w(rows, headers)
    fmt = "  " + "  ".join(f"{{:<{n}}}" for n in w)
    print(fmt.format(*headers))
    print("  " + "  ".join("─" * n for n in w))
    for row in rows:
        line = fmt.format(*[str(c) for c in row])
        if warn_col is not None and row[warn_col]:
            line = YELLOW(line)
        elif color_col is not None and row[color_col]:
            line = GREEN(line)
        print(line)

# ── instance catalogs (loaded from CSV price lists) ───────────────────────────
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hpc-cost-comparator"))
from load_pricelist import GPU_MODEL_MAP, load_catalogs as _load_catalogs
del _sys

AWS_INSTANCES, AZURE_INSTANCES = _load_catalogs()


# ── instance matching (mirror of app.py find_best_instance) ──────────────────
def find_best_instance(catalog, cpus, mem_mb, gpu_count, gpu_model):
    mem_gb   = mem_mb / 1024.0
    norm_gpu = GPU_MODEL_MAP.get(gpu_model.lower(), gpu_model.upper()) if gpu_model else ""
    # When model is unknown require a real GPU vendor so Inferentia/Trainium are excluded
    def _real_gpu(i): return (i["gpu_model"] == norm_gpu) if norm_gpu else (i.get("gpu_vendor") in ("nvidia", "amd"))
    if gpu_count > 0:
        for passes in [
            lambda i: i["gpu_count"] >= gpu_count and _real_gpu(i) and i["vcpus"] >= max(cpus,1) and i["mem_gb"] >= mem_gb,
            lambda i: i["gpu_count"] >= gpu_count and _real_gpu(i) and i["vcpus"] >= max(cpus,1),
            lambda i: i["gpu_count"] >= gpu_count and i.get("gpu_vendor") == "nvidia" and i["vcpus"] >= max(cpus,1),
            lambda i: i["gpu_count"] >= gpu_count and i.get("gpu_vendor") in ("nvidia", "amd") and i["vcpus"] >= max(cpus,1),
            lambda i: i["gpu_count"] >= gpu_count and i.get("gpu_vendor") in ("nvidia", "amd"),
        ]:
            c = [i for i in catalog if passes(i)]
            if c:
                return min(c, key=lambda x: x["price"])
    for passes in [
        lambda i: i["gpu_count"] == 0 and i["vcpus"] >= max(cpus,1) and i["mem_gb"] >= mem_gb,
        lambda i: i["gpu_count"] == 0 and i["vcpus"] >= max(cpus,1),
        lambda i: i["gpu_count"] == 0,
    ]:
        c = [i for i in catalog if passes(i)]
        if c:
            return min(c, key=lambda x: x["price"])
    return min(catalog, key=lambda x: x["price"])

# ── DB helpers ────────────────────────────────────────────────────────────────
def open_db(path):
    if not os.path.exists(path):
        print(RED(f"  ERROR: database not found: {path}"))
        sys.exit(1)
    conn = sqlite3.connect(path, timeout=30)  # wait up to 30 s if app is writing
    conn.row_factory = sqlite3.Row
    return conn

def build_where(args):
    """Return (where_clause, params) for date + cluster filters."""
    conds, params = [], []
    if args.start:
        try:
            ts = int(datetime.strptime(args.start, "%Y-%m-%d").timestamp())
            conds.append("start_time >= ?"); params.append(ts)
        except ValueError:
            print(RED(f"  Bad --start date: {args.start}")); sys.exit(1)
    if args.end:
        try:
            ts = int((datetime.strptime(args.end, "%Y-%m-%d") + timedelta(days=1)).timestamp())
            conds.append("start_time < ?"); params.append(ts)
        except ValueError:
            print(RED(f"  Bad --end date: {args.end}")); sys.exit(1)
    if args.cluster and args.cluster != "all":
        conds.append("cluster = ?"); params.append(args.cluster)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params

# ─────────────────────────────────────────────────────────────────────────────
# 1. SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def cmd_summary(args):
    section("DATABASE SUMMARY")
    conn  = open_db(args.db)
    where, params = build_where(args)

    r = conn.execute(f"""
        SELECT COUNT(*) AS jobs,
               COUNT(DISTINCT username) AS users,
               COUNT(DISTINCT cluster)  AS clusters,
               MIN(start_time)          AS first_ts,
               MAX(start_time)          AS last_ts,
               SUM(elapsed_min)         AS total_min,
               SUM(aws_total)           AS aws,
               SUM(azure_total)         AS azure,
               SUM(CASE WHEN gpu_count>0 THEN 1 ELSE 0 END) AS gpu_jobs,
               SUM(CASE WHEN gpu_count=0 THEN 1 ELSE 0 END) AS cpu_jobs,
               SUM(CASE WHEN aws_total=0 AND azure_total=0 THEN 1 ELSE 0 END) AS zero_cost
        FROM jobs {where}
    """, params).fetchone()

    first = datetime.fromtimestamp(r["first_ts"]).strftime("%Y-%m-%d") if r["first_ts"] else "—"
    last  = datetime.fromtimestamp(r["last_ts"]).strftime("%Y-%m-%d")  if r["last_ts"]  else "—"

    print(f"  Database       : {args.db}")
    print(f"  Filter         : cluster={args.cluster or 'all'}  start={args.start or '—'}  end={args.end or '—'}")
    print(f"  Date range     : {first}  →  {last}")
    print()
    print(f"  Total jobs     : {r['jobs']:,}")
    print(f"    GPU jobs     : {r['gpu_jobs']:,}  ({pct(r['gpu_jobs'], r['jobs'])})")
    print(f"    CPU jobs     : {r['cpu_jobs']:,}  ({pct(r['cpu_jobs'], r['jobs'])})")
    print(f"  Unique users   : {r['users']:,}")
    print(f"  Clusters       : {r['clusters']}")
    print(f"  Compute hours  : {fmt_hours(r['total_min'] or 0)}")
    print()
    print(f"  AWS total      : {fmt_money(r['aws']   or 0)}")
    print(f"  Azure total    : {fmt_money(r['azure'] or 0)}")

    if r["zero_cost"]:
        print()
        print(YELLOW(f"  ⚠  {r['zero_cost']:,} jobs have $0.00 cost — run 'anomalies' for details"))

    # Cluster breakdown
    rows = conn.execute(f"""
        SELECT cluster,
               COUNT(*) AS jobs,
               SUM(CASE WHEN gpu_count>0 THEN 1 ELSE 0 END) AS gpu_jobs,
               ROUND(SUM(elapsed_min)/60.0,0) AS hours,
               ROUND(SUM(aws_total),2)   AS aws,
               ROUND(SUM(azure_total),2) AS azure
        FROM jobs {where}
        GROUP BY cluster ORDER BY aws DESC
    """, params).fetchall()

    print()
    print(BOLD("  By cluster:"))
    print_table(
        ["Cluster", "Jobs", "GPU Jobs", "Compute Hrs", "AWS Total", "Azure Total"],
        [(r["cluster"], f"{r['jobs']:,}", f"{r['gpu_jobs']:,}",
          f"{r['hours']:,.0f}", fmt_money(r["aws"]), fmt_money(r["azure"]))
         for r in rows]
    )
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# 2. USERS
# ─────────────────────────────────────────────────────────────────────────────
def cmd_users(args):
    section("PER-USER BREAKDOWN")
    conn  = open_db(args.db)
    where, params = build_where(args)
    top   = args.top

    rows = conn.execute(f"""
        SELECT username,
               COUNT(*)                              AS jobs,
               SUM(CASE WHEN gpu_count>0 THEN 1 ELSE 0 END) AS gpu_jobs,
               ROUND(SUM(elapsed_min)/60.0, 1)      AS hours,
               ROUND(SUM(aws_total),   2)            AS aws,
               ROUND(SUM(azure_total), 2)            AS azure,
               MAX(req_cpus)                         AS max_cpus,
               ROUND(MAX(req_mem_mb)/1024.0, 0)      AS max_mem_gb,
               ROUND(AVG(elapsed_min), 0)            AS avg_min
        FROM jobs {where}
        GROUP BY username
        ORDER BY hours DESC
        LIMIT ?
    """, params + [top]).fetchall()

    total_aws   = sum(r["aws"]   for r in rows)
    total_azure = sum(r["azure"] for r in rows)
    total_hours = sum(r["hours"] for r in rows)

    print(f"  Showing top {top} users by compute hours")
    print_table(
        ["Username", "Jobs", "GPU Jobs", "Hours", "AWS Cost", "Azure Cost", "Max CPUs", "Max Mem GB", "Avg Job (min)"],
        [(r["username"], f"{r['jobs']:,}", f"{r['gpu_jobs']:,}",
          f"{r['hours']:,.1f}", fmt_money(r["aws"]), fmt_money(r["azure"]),
          r["max_cpus"], f"{r['max_mem_gb']:.0f}", f"{r['avg_min']:.0f}")
         for r in rows]
    )
    print()
    print(f"  Totals (top {top}): {total_hours:,.1f} hrs  |  AWS {fmt_money(total_aws)}  |  Azure {fmt_money(total_azure)}")

    # Users with GPU jobs > 0 but $0 AWS cost (possible detection gap)
    bad = conn.execute(f"""
        SELECT username, COUNT(*) AS cnt
        FROM jobs {where}
          {'AND' if where else 'WHERE'} gpu_count > 0 AND aws_total = 0
        GROUP BY username ORDER BY cnt DESC LIMIT 10
    """, params).fetchall()
    if bad:
        print()
        print(YELLOW("  ⚠  Users with GPU jobs but $0 cost (possible mis-priced records):"))
        print_table(["Username", "Affected Jobs"], [(r["username"], r["cnt"]) for r in bad])
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# 3. CLUSTER
# ─────────────────────────────────────────────────────────────────────────────
def cmd_cluster(args):
    section("PER-CLUSTER BREAKDOWN")
    conn  = open_db(args.db)
    where, params = build_where(args)

    rows = conn.execute(f"""
        SELECT cluster,
               COUNT(*)                              AS jobs,
               COUNT(DISTINCT username)              AS users,
               SUM(CASE WHEN gpu_count>0 THEN 1 ELSE 0 END)  AS gpu_jobs,
               ROUND(SUM(elapsed_min)/60.0, 1)      AS hours,
               ROUND(AVG(req_cpus), 1)               AS avg_cpus,
               ROUND(AVG(req_mem_mb)/1024.0, 1)      AS avg_mem_gb,
               ROUND(AVG(CASE WHEN gpu_count>0 THEN gpu_count END), 2) AS avg_gpus,
               ROUND(SUM(aws_total),   2)            AS aws,
               ROUND(SUM(azure_total), 2)            AS azure
        FROM jobs {where}
        GROUP BY cluster ORDER BY jobs DESC
    """, params).fetchall()

    print_table(
        ["Cluster", "Jobs", "Users", "GPU Jobs", "Hours", "Avg CPUs", "Avg Mem GB", "Avg GPUs", "AWS Total", "Azure Total"],
        [(r["cluster"], f"{r['jobs']:,}", f"{r['users']:,}", f"{r['gpu_jobs']:,}",
          f"{r['hours']:,.1f}", f"{r['avg_cpus']:.1f}", f"{r['avg_mem_gb']:.1f}",
          f"{r['avg_gpus'] or 0:.2f}", fmt_money(r["aws"]), fmt_money(r["azure"]))
         for r in rows]
    )

    # Monthly trend per cluster
    print()
    print(BOLD("  Monthly job volume by cluster:"))
    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', start_time, 'unixepoch') AS month,
               cluster,
               COUNT(*) AS jobs,
               ROUND(SUM(aws_total), 2) AS aws
        FROM jobs {where}
        GROUP BY month, cluster
        ORDER BY month DESC, aws DESC
        LIMIT 36
    """, params).fetchall()
    print_table(
        ["Month", "Cluster", "Jobs", "AWS Total"],
        [(r["month"], r["cluster"], f"{r['jobs']:,}", fmt_money(r["aws"])) for r in monthly]
    )
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# 4. CLOUD (AWS vs Azure)
# ─────────────────────────────────────────────────────────────────────────────
def cmd_cloud(args):
    section("AWS vs AZURE COMPARISON")
    conn  = open_db(args.db)
    where, params = build_where(args)

    totals = conn.execute(f"""
        SELECT SUM(aws_total) AS aws, SUM(azure_total) AS azure,
               COUNT(*) AS jobs
        FROM jobs {where}
    """, params).fetchone()
    aws_total   = totals["aws"]   or 0
    azure_total = totals["azure"] or 0

    delta = aws_total - azure_total
    print(f"  AWS   total  : {fmt_money(aws_total)}")
    print(f"  Azure total  : {fmt_money(azure_total)}")
    print(f"  Delta (AWS−Az): {fmt_money(abs(delta))}  ({'AWS cheaper' if delta < 0 else 'Azure cheaper' if delta > 0 else 'equal'})")
    print(f"  Per-job avg  : AWS {fmt_money(aws_total/totals['jobs'] if totals['jobs'] else 0)}  /  Azure {fmt_money(azure_total/totals['jobs'] if totals['jobs'] else 0)}")

    # Instance distribution — AWS
    print()
    print(BOLD("  Most-used AWS instances:"))
    aws_inst = conn.execute(f"""
        SELECT aws_instance,
               COUNT(*) AS jobs,
               ROUND(SUM(aws_total), 2) AS total,
               ROUND(AVG(aws_total), 4) AS avg_cost
        FROM jobs {where}
        GROUP BY aws_instance ORDER BY jobs DESC LIMIT 15
    """, params).fetchall()
    print_table(
        ["Instance", "Jobs", "Total Cost", "Avg Cost/Job"],
        [(r["aws_instance"], f"{r['jobs']:,}", fmt_money(r["total"]), fmt_money(r["avg_cost"]))
         for r in aws_inst]
    )

    # Instance distribution — Azure
    print()
    print(BOLD("  Most-used Azure instances:"))
    az_inst = conn.execute(f"""
        SELECT azure_instance,
               COUNT(*) AS jobs,
               ROUND(SUM(azure_total), 2) AS total,
               ROUND(AVG(azure_total), 4) AS avg_cost
        FROM jobs {where}
        GROUP BY azure_instance ORDER BY jobs DESC LIMIT 15
    """, params).fetchall()
    print_table(
        ["Instance", "Jobs", "Total Cost", "Avg Cost/Job"],
        [(r["azure_instance"], f"{r['jobs']:,}", fmt_money(r["total"]), fmt_money(r["avg_cost"]))
         for r in az_inst]
    )

    # GPU-specific cloud costs
    print()
    print(BOLD("  GPU job cloud costs by model:"))
    gpu_cloud = conn.execute(f"""
        SELECT gpu_model,
               COUNT(*) AS jobs,
               ROUND(SUM(aws_total), 2)   AS aws,
               ROUND(SUM(azure_total), 2) AS azure,
               aws_instance,
               azure_instance
        FROM jobs {where}
          {'AND' if where else 'WHERE'} gpu_count > 0
        GROUP BY gpu_model ORDER BY aws DESC
    """, params).fetchall()
    print_table(
        ["GPU Model", "Jobs", "AWS Total", "Azure Total", "AWS Instance", "Azure Instance"],
        [(r["gpu_model"] or "(none)", f"{r['jobs']:,}", fmt_money(r["aws"]), fmt_money(r["azure"]),
          r["aws_instance"], r["azure_instance"])
         for r in gpu_cloud]
    )
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# 5. GPU
# ─────────────────────────────────────────────────────────────────────────────
def cmd_gpu(args):
    section("GPU ANALYSIS")
    conn  = open_db(args.db)
    where, params = build_where(args)

    totals = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN gpu_count>0 THEN 1 ELSE 0 END) AS gpu_jobs,
               SUM(CASE WHEN gpu_count=0 THEN 1 ELSE 0 END) AS cpu_jobs,
               SUM(CASE WHEN gpu_count>0 THEN aws_total   ELSE 0 END) AS gpu_aws,
               SUM(CASE WHEN gpu_count=0 THEN aws_total   ELSE 0 END) AS cpu_aws,
               SUM(CASE WHEN gpu_count>0 THEN azure_total ELSE 0 END) AS gpu_azure,
               SUM(CASE WHEN gpu_count=0 THEN azure_total ELSE 0 END) AS cpu_azure,
               SUM(CASE WHEN gpu_count>0 THEN elapsed_min ELSE 0 END) AS gpu_min,
               SUM(CASE WHEN gpu_count=0 THEN elapsed_min ELSE 0 END) AS cpu_min
        FROM jobs {where}
    """, params).fetchone()

    t = totals["total"] or 1
    print(f"  GPU jobs     : {totals['gpu_jobs']:,}  ({pct(totals['gpu_jobs'], t)} of all jobs)")
    print(f"  CPU jobs     : {totals['cpu_jobs']:,}  ({pct(totals['cpu_jobs'], t)})")
    print()
    print(f"  GPU compute hours : {fmt_hours(totals['gpu_min'] or 0)}")
    print(f"  CPU compute hours : {fmt_hours(totals['cpu_min'] or 0)}")
    print()
    print(f"  GPU AWS cost  : {fmt_money(totals['gpu_aws']   or 0)}  ({pct(totals['gpu_aws']   or 0, (totals['gpu_aws'] or 0) + (totals['cpu_aws']   or 0))} of AWS total)")
    print(f"  CPU AWS cost  : {fmt_money(totals['cpu_aws']   or 0)}")
    print(f"  GPU Azure cost: {fmt_money(totals['gpu_azure'] or 0)}  ({pct(totals['gpu_azure'] or 0, (totals['gpu_azure'] or 0) + (totals['cpu_azure'] or 0))} of Azure total)")
    print(f"  CPU Azure cost: {fmt_money(totals['cpu_azure'] or 0)}")

    # GPU model distribution
    print()
    print(BOLD("  GPU model distribution:"))
    models = conn.execute(f"""
        SELECT gpu_model, gpu_count,
               COUNT(*) AS jobs,
               ROUND(SUM(elapsed_min)/60.0, 1) AS hours,
               ROUND(SUM(aws_total),   2) AS aws,
               ROUND(SUM(azure_total), 2) AS azure,
               aws_instance
        FROM jobs {where}
          {'AND' if where else 'WHERE'} gpu_count > 0
        GROUP BY gpu_model, gpu_count
        ORDER BY jobs DESC
    """, params).fetchall()
    print_table(
        ["GPU Model", "Count/Job", "Jobs", "Hours", "AWS Total", "Azure Total", "Mapped→Instance"],
        [(r["gpu_model"] or "(none)", r["gpu_count"], f"{r['jobs']:,}",
          f"{r['hours']:,.1f}", fmt_money(r["aws"]), fmt_money(r["azure"]), r["aws_instance"])
         for r in models]
    )

    # GPU jobs with unknown model  (model-free → may be mis-mapped)
    no_model = conn.execute(f"""
        SELECT COUNT(*) AS cnt, ROUND(SUM(aws_total), 2) AS aws
        FROM jobs {where}
          {'AND' if where else 'WHERE'} gpu_count > 0 AND (gpu_model = '' OR gpu_model IS NULL)
    """, params).fetchone()
    if no_model["cnt"]:
        print()
        print(YELLOW(f"  ⚠  {no_model['cnt']:,} GPU jobs have no model stored (gpu_model='')."))
        print(f"     Instance selection falls back to cheapest available GPU instance.")
        print(f"     Total AWS cost for these jobs: {fmt_money(no_model['aws'])}.")
        print(f"     Run: python3 import_history.py --patch-gpu-model <model> --db {args.db}")
        print(f"     to assign a model and recalculate costs for records not in the CSV dump.")

    # GPU cost per hour efficiency
    print()
    print(BOLD("  Cost per GPU-hour by model (AWS):"))
    eff = conn.execute(f"""
        SELECT gpu_model,
               ROUND(SUM(aws_total) / (SUM(elapsed_min)/60.0), 4) AS aws_per_gpu_hour,
               aws_instance,
               COUNT(*) AS jobs
        FROM jobs {where}
          {'AND' if where else 'WHERE'} gpu_count > 0 AND elapsed_min > 0
        GROUP BY gpu_model
        ORDER BY aws_per_gpu_hour DESC
    """, params).fetchall()
    print_table(
        ["GPU Model", "$/GPU-hour (AWS)", "Instance Used", "Jobs"],
        [(r["gpu_model"] or "(none)", f"${r['aws_per_gpu_hour']:.4f}",
          r["aws_instance"], f"{r['jobs']:,}")
         for r in eff]
    )
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# 6. CPU ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
def cmd_cpu(args):
    section("CPU-ONLY JOB ANALYSIS")
    conn  = open_db(args.db)
    where, params = build_where(args)

    # Add cpu_only filter for all subsequent queries
    cpu_where  = (where + " AND gpu_count = 0") if where else "WHERE gpu_count = 0"
    cpu_params = params[:]

    # ── overview ──────────────────────────────────────────────────────────────
    totals = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN gpu_count=0 THEN 1 ELSE 0 END) AS cpu_jobs,
               SUM(CASE WHEN gpu_count>0 THEN 1 ELSE 0 END) AS gpu_jobs,
               SUM(CASE WHEN gpu_count=0 THEN elapsed_min ELSE 0 END) AS cpu_min,
               SUM(CASE WHEN gpu_count=0 THEN aws_total   ELSE 0 END) AS cpu_aws,
               SUM(CASE WHEN gpu_count=0 THEN azure_total ELSE 0 END) AS cpu_azure,
               SUM(aws_total)   AS all_aws,
               SUM(azure_total) AS all_azure
        FROM jobs {where}
    """, params).fetchone()

    t = totals["total"] or 1
    print(f"  CPU jobs       : {totals['cpu_jobs']:,}  ({pct(totals['cpu_jobs'], t)} of all jobs)")
    print(f"  GPU jobs       : {totals['gpu_jobs']:,}  ({pct(totals['gpu_jobs'], t)})")
    print()
    print(f"  CPU compute    : {fmt_hours(totals['cpu_min'] or 0)}")
    print(f"  CPU AWS cost   : {fmt_money(totals['cpu_aws']   or 0)}  ({pct(totals['cpu_aws']   or 0, totals['all_aws']   or 1)} of AWS total)")
    print(f"  CPU Azure cost : {fmt_money(totals['cpu_azure'] or 0)}  ({pct(totals['cpu_azure'] or 0, totals['all_azure'] or 1)} of Azure total)")

    # ── vCPU distribution ─────────────────────────────────────────────────────
    print()
    print(BOLD("  vCPU count distribution (CPU jobs only):"))
    vcpu_rows = conn.execute(f"""
        SELECT CASE
                 WHEN req_cpus <=  4 THEN '1–4   (serial)'
                 WHEN req_cpus <=  8 THEN '5–8   (small parallel)'
                 WHEN req_cpus <= 16 THEN '9–16  (medium parallel)'
                 WHEN req_cpus <= 32 THEN '17–32 (large parallel)'
                 ELSE                     '33+   (MPI / large)'
               END AS bucket,
               COUNT(*) AS jobs,
               ROUND(SUM(elapsed_min)/60.0, 1) AS hours,
               ROUND(SUM(aws_total),   2) AS aws,
               ROUND(SUM(azure_total), 2) AS azure
        FROM jobs {cpu_where}
        GROUP BY bucket
        ORDER BY MIN(req_cpus)
    """, cpu_params).fetchall()
    print_table(
        ["vCPU Range", "Jobs", "Compute Hrs", "AWS Total", "Azure Total"],
        [(r["bucket"], f"{r['jobs']:,}", f"{r['hours']:,.1f}",
          fmt_money(r["aws"]), fmt_money(r["azure"]))
         for r in vcpu_rows]
    )

    # ── memory distribution ───────────────────────────────────────────────────
    print()
    print(BOLD("  Memory distribution (CPU jobs only):"))
    mem_rows = conn.execute(f"""
        SELECT CASE
                 WHEN req_mem_mb <=  8192 THEN '≤ 8 GB'
                 WHEN req_mem_mb <= 32768 THEN '8–32 GB'
                 WHEN req_mem_mb <= 131072 THEN '32–128 GB'
                 ELSE                          '> 128 GB'
               END AS bucket,
               COUNT(*) AS jobs,
               ROUND(SUM(elapsed_min)/60.0, 1) AS hours,
               ROUND(SUM(aws_total),   2) AS aws
        FROM jobs {cpu_where}
        GROUP BY bucket
        ORDER BY MIN(req_mem_mb)
    """, cpu_params).fetchall()
    print_table(
        ["Memory Range", "Jobs", "Compute Hrs", "AWS Total"],
        [(r["bucket"], f"{r['jobs']:,}", f"{r['hours']:,.1f}", fmt_money(r["aws"]))
         for r in mem_rows]
    )

    # ── instance mapping distribution ─────────────────────────────────────────
    print()
    print(BOLD(f"  Top {args.top} cloud instances used for CPU jobs (AWS):"))
    inst_rows = conn.execute(f"""
        SELECT aws_instance,
               COUNT(*) AS jobs,
               ROUND(SUM(elapsed_min)/60.0, 1) AS hours,
               ROUND(SUM(aws_total),   2) AS aws,
               ROUND(SUM(azure_total), 2) AS azure
        FROM jobs {cpu_where}
        GROUP BY aws_instance
        ORDER BY jobs DESC
        LIMIT ?
    """, cpu_params + [args.top]).fetchall()
    print_table(
        ["AWS Instance", "Jobs", "Compute Hrs", "AWS Total", "Azure Total"],
        [(r["aws_instance"] or "(none)", f"{r['jobs']:,}", f"{r['hours']:,.1f}",
          fmt_money(r["aws"]), fmt_money(r["azure"]))
         for r in inst_rows]
    )

    # ── cost per CPU-hour ─────────────────────────────────────────────────────
    print()
    print(BOLD("  Cost per CPU-hour by instance (AWS, top instances by job count):"))
    eff_rows = conn.execute(f"""
        SELECT aws_instance,
               COUNT(*) AS jobs,
               ROUND(SUM(aws_total) / MAX(1, SUM(elapsed_min * req_cpus) / 60.0), 4)
                   AS aws_per_cpu_hr
        FROM jobs {cpu_where}
          AND elapsed_min > 0 AND req_cpus > 0
        GROUP BY aws_instance
        ORDER BY jobs DESC
        LIMIT ?
    """, cpu_params + [args.top]).fetchall()
    print_table(
        ["AWS Instance", "Jobs", "$/CPU-hour"],
        [(r["aws_instance"] or "(none)", f"{r['jobs']:,}", f"${r['aws_per_cpu_hr']:.4f}")
         for r in eff_rows]
    )

    # ── spot-check ────────────────────────────────────────────────────────────
    print()
    print(BOLD("  Spot-check: 10 random CPU jobs — stored cost vs catalog recalculation"))
    sample = conn.execute(f"""
        SELECT job_id, cluster, req_cpus, req_mem_mb,
               time_limit_min, elapsed_min,
               COALESCE(num_nodes, 1) AS num_nodes,
               aws_instance, aws_total, azure_instance, azure_total
        FROM jobs {cpu_where}
        ORDER BY RANDOM() LIMIT 10
    """, cpu_params).fetchall()

    headers = ["Job ID", "CPUs", "Mem GB", "Nodes", "Hours",
               "AWS Inst", "DB $", "Calc $", "Δ", "OK?"]
    table_rows = []
    issues = 0
    for r in sample:
        th = (r["time_limit_min"] or r["elapsed_min"]) / 60.0
        n  = r["num_nodes"] or 1
        inst   = find_best_instance(AWS_INSTANCES, r["req_cpus"], r["req_mem_mb"], 0, "")
        calc   = round(inst["price"] * th * n, 4)
        stored = round(r["aws_total"], 4)
        diff   = abs(calc - stored)
        ok = "✓" if diff < 0.01 else "✗"
        if ok == "✗":
            issues += 1
        table_rows.append([
            r["job_id"][:16], r["req_cpus"], f"{r['req_mem_mb']/1024:.0f}",
            n, f"{th:.2f}", inst["name"][:22],
            f"${stored:.4f}", f"${calc:.4f}",
            f"${diff:.4f}" if diff >= 0.01 else "—", ok,
        ])
    print_table(headers, table_rows)
    if issues:
        print(YELLOW(f"\n  ⚠  {issues} jobs have stored cost ≠ recalculated cost > $0.01 — run 'recalc-check' for full audit"))
    else:
        print(GREEN("\n  ✓  All spot-checked CPU jobs match catalog pricing"))

    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 7. PRICING VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
def cmd_pricing(args):
    section("CLOUD PRICING VALIDATION")
    conn  = open_db(args.db)
    where, params = build_where(args)

    # Build lookup maps from catalogs
    aws_price   = {i["name"]: i["price"] for i in AWS_INSTANCES}
    azure_price = {i["name"]: i["price"] for i in AZURE_INSTANCES}

    # Instances in DB not in catalog (stale / renamed)
    db_aws = conn.execute(f"SELECT DISTINCT aws_instance FROM jobs {where}", params).fetchall()
    db_az  = conn.execute(f"SELECT DISTINCT azure_instance FROM jobs {where}", params).fetchall()

    missing_aws = [r["aws_instance"] for r in db_aws if r["aws_instance"] and r["aws_instance"] not in aws_price]
    missing_az  = [r["azure_instance"] for r in db_az if r["azure_instance"] and r["azure_instance"] not in azure_price]

    if missing_aws:
        print(RED(f"  ✗  {len(missing_aws)} AWS instances in DB are NOT in the current catalog:"))
        for n in missing_aws: print(f"       {n}")
    else:
        print(GREEN("  ✓  All DB AWS instances are present in the catalog"))

    if missing_az:
        print(RED(f"  ✗  {len(missing_az)} Azure instances in DB are NOT in the current catalog:"))
        for n in missing_az: print(f"       {n}")
    else:
        print(GREEN("  ✓  All DB Azure instances are present in the catalog"))

    # Sample spot-check: for 10 random GPU jobs, recompute expected price vs stored
    print()
    print(BOLD("  Spot-check: 10 random GPU jobs — stored price vs catalog price"))
    rows = conn.execute(f"""
        SELECT job_id, cluster, req_cpus, req_mem_mb, gpu_count, gpu_model,
               time_limit_min, elapsed_min,
               aws_instance, aws_total, azure_instance, azure_total
        FROM jobs {where}
          {'AND' if where else 'WHERE'} gpu_count > 0
        ORDER BY RANDOM() LIMIT 10
    """, params).fetchall()

    headers = ["Job ID", "CPUs", "Mem GB", "GPUs", "Model", "Hours",
               "AWS Inst", "DB $", "Calc $", "Δ AWS", "OK?"]
    table_rows = []
    issues = 0
    for r in rows:
        th = (r["time_limit_min"] or r["elapsed_min"]) / 60.0
        inst = find_best_instance(AWS_INSTANCES, r["req_cpus"], r["req_mem_mb"],
                                  r["gpu_count"], r["gpu_model"])
        calc = round(inst["price"] * th, 4)
        stored = round(r["aws_total"], 4)
        diff   = abs(calc - stored)
        ok = "✓" if diff < 0.01 else "✗"
        if ok == "✗": issues += 1
        table_rows.append([
            r["job_id"][:16], r["req_cpus"], f"{r['req_mem_mb']/1024:.0f}",
            r["gpu_count"], r["gpu_model"] or "—",
            f"{th:.2f}", inst["name"][:22],
            f"${stored:.4f}", f"${calc:.4f}",
            f"${diff:.4f}" if diff >= 0.01 else "—", ok
        ])
    print_table(headers, table_rows)
    if issues:
        print(YELLOW(f"\n  ⚠  {issues} jobs have stored cost ≠ recalculated cost > $0.01 — run 'recalc-check' for full audit"))
    else:
        print(GREEN("\n  ✓  All spot-checked jobs match catalog pricing"))

    # Show full catalog summary
    print()
    print(BOLD("  AWS catalog summary:"))
    aws_by_model = {}
    for i in AWS_INSTANCES:
        m = i["gpu_model"] or "CPU"
        aws_by_model.setdefault(m, []).append(i)
    print_table(
        ["GPU Model", "# Instances", "Price Range ($/hr)"],
        [(model, len(insts),
          f"${min(i['price'] for i in insts):.3f} – ${max(i['price'] for i in insts):.3f}")
         for model, insts in sorted(aws_by_model.items())]
    )

    print()
    print(BOLD("  Azure catalog summary:"))
    az_by_model = {}
    for i in AZURE_INSTANCES:
        m = i["gpu_model"] or "CPU"
        az_by_model.setdefault(m, []).append(i)
    print_table(
        ["GPU Model", "# Instances", "Price Range ($/hr)"],
        [(model, len(insts),
          f"${min(i['price'] for i in insts):.3f} – ${max(i['price'] for i in insts):.3f}")
         for model, insts in sorted(az_by_model.items())]
    )
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# 7. RECALC-CHECK
# ─────────────────────────────────────────────────────────────────────────────
def cmd_recalc_check(args):
    section("COST RECALCULATION AUDIT")
    conn  = open_db(args.db)
    where, params = build_where(args)

    print("  Recalculating all stored costs against current catalog pricing …")
    rows = conn.execute(f"""
        SELECT job_id, cluster, username, req_cpus, req_mem_mb, gpu_count, gpu_model,
               time_limit_min, elapsed_min,
               aws_instance, aws_total, azure_instance, azure_total,
               COALESCE(num_nodes, 1) AS num_nodes
        FROM jobs {where}
    """, params).fetchall()

    total = len(rows)
    aws_mismatch = aws_delta_total = 0.0
    az_mismatch  = az_delta_total  = 0.0
    wrong_inst_aws = wrong_inst_az  = 0
    zero_gpu_cost  = 0
    mismatch_rows  = []

    for r in rows:
        th = (r["time_limit_min"] or r["elapsed_min"]) / 60.0
        nn = r["num_nodes"] or 1
        expected_aws  = find_best_instance(AWS_INSTANCES,   r["req_cpus"], r["req_mem_mb"], r["gpu_count"], r["gpu_model"])
        expected_az   = find_best_instance(AZURE_INSTANCES, r["req_cpus"], r["req_mem_mb"], r["gpu_count"], r["gpu_model"])
        calc_aws   = round(expected_aws["price"]  * th * nn, 4)
        calc_az    = round(expected_az["price"]   * th * nn, 4)
        delta_aws  = abs(calc_aws  - (r["aws_total"]   or 0))
        delta_az   = abs(calc_az   - (r["azure_total"] or 0))

        if delta_aws  > 0.01: aws_mismatch += 1; aws_delta_total += delta_aws
        if delta_az   > 0.01: az_mismatch  += 1; az_delta_total  += delta_az
        if expected_aws["name"]  != r["aws_instance"]:   wrong_inst_aws += 1
        if expected_az["name"]   != r["azure_instance"]: wrong_inst_az  += 1
        if r["gpu_count"] > 0 and (r["aws_total"] or 0) == 0: zero_gpu_cost += 1

        if delta_aws > 0.01 and len(mismatch_rows) < args.top:
            mismatch_rows.append([
                r["job_id"][:16], r["cluster"], r["username"][:12],
                r["gpu_count"], r["gpu_model"] or "—",
                f"${r['aws_total']:.4f}", f"${calc_aws:.4f}",
                f"${delta_aws:.4f}",
                r["aws_instance"][:22], expected_aws["name"][:22]
            ])

    print(f"  Total records checked : {total:,}")
    print()

    sym = GREEN("✓") if aws_mismatch == 0 else YELLOW("⚠")
    print(f"  {sym}  AWS   cost mismatches : {aws_mismatch:,} jobs  (cumulative delta: {fmt_money(aws_delta_total)})")
    sym = GREEN("✓") if az_mismatch  == 0 else YELLOW("⚠")
    print(f"  {sym}  Azure cost mismatches : {az_mismatch:,} jobs  (cumulative delta: {fmt_money(az_delta_total)})")
    sym = RED("✗")  if zero_gpu_cost > 0 else GREEN("✓")
    print(f"  {sym}  GPU jobs with $0 cost : {zero_gpu_cost:,}")
    print(f"     AWS  instance mismatch: {wrong_inst_aws:,}  |  Azure instance mismatch: {wrong_inst_az:,}")

    if mismatch_rows:
        print()
        print(BOLD(f"  First {len(mismatch_rows)} AWS cost mismatches:"))
        print_table(
            ["Job ID", "Cluster", "User", "GPUs", "Model",
             "Stored $", "Calc $", "Delta", "Stored Instance", "Expected Instance"],
            mismatch_rows
        )

    if aws_mismatch > 0 or zero_gpu_cost > 0:
        print()
        print(YELLOW("  Recommendation: re-run dump_history.sh + import_history.py --force-update"))
        print("  to recalculate costs with correct GPU detection and current pricing.")
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# 8. LIVE CHECK (API + Slurm)
# ─────────────────────────────────────────────────────────────────────────────
def cmd_live(args):
    section("LIVE DATA VALIDATION")

    # ── API check ─────────────────────────────────────────────────────────────
    try:
        import urllib.request
        base = args.api.rstrip("/")
        with urllib.request.urlopen(f"{base}/api/metrics", timeout=5) as resp:
            metrics = json.loads(resp.read())
        print(GREEN(f"  ✓  API reachable: {base}"))
        print(f"     Active jobs        : {metrics.get('active_jobs', '—')}")
        print(f"     Projected AWS cost : {fmt_money(metrics.get('projected_cost_aws', 0))}")
        print(f"     Projected Az cost  : {fmt_money(metrics.get('projected_cost_azure', 0))}")
        print(f"     Last updated       : {metrics.get('last_updated', '—')}")

        with urllib.request.urlopen(f"{base}/api/job-details", timeout=5) as resp:
            details = json.loads(resp.read())
        jobs = details.get("jobs", [])
        gpu_jobs = [j for j in jobs if j.get("gpu_count", 0) > 0]
        print(f"     GPU jobs in API    : {len(gpu_jobs)} / {len(jobs)}")

        if gpu_jobs:
            print()
            print(BOLD("  Sample GPU jobs from live API:"))
            print_table(
                ["Job ID", "CPUs", "Mem GB", "GPUs", "GPU Model", "AWS Instance", "AWS $/hr", "Azure $/hr"],
                [(j["job_id"], j.get("cpus","—"), j.get("mem_gb","—"),
                  j.get("gpu_count","—"), j.get("gpu_model","—"),
                  j.get("aws_instance","—"), f"${j.get('aws_hourly',0):.3f}",
                  f"${j.get('azure_hourly',0):.3f}")
                 for j in gpu_jobs[:10]]
            )
    except Exception as e:
        print(YELLOW(f"  ⚠  API not reachable at {args.api}: {e}"))
        print("     Start the dashboard server and re-run, or use --api to set the URL.")

    # ── Slurm live check ──────────────────────────────────────────────────────
    slurm_bin = args.slurm_bin or os.environ.get("SLURM_BIN_DIR", "")
    def slurm_cmd(name):
        return os.path.join(slurm_bin, name) if slurm_bin else name

    print()
    print(BOLD("  Live Slurm validation:"))

    # squeue running jobs
    try:
        r = subprocess.run(
            [slurm_cmd("squeue"), "-h", "-t", "RUNNING", "-o", "%i|%C|%m|%b|%l|%N"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
        )
        lines = [l for l in r.stdout.decode().splitlines() if l.strip()]
        print(f"  squeue RUNNING jobs  : {len(lines):,}")

        # sacct GPU detection
        sacct_r = subprocess.run(
            [slurm_cmd("sacct"), "-a", "-P", "--noheader", "--state=RUNNING",
             "--format=JobID,ReqTRES"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
        )
        gpu_from_sacct = sum(
            1 for l in sacct_r.stdout.decode().splitlines()
            if "gres/gpu" in l and "." not in l.split("|")[0]
        )
        print(f"  sacct GPU jobs live  : {gpu_from_sacct:,}")
        print(f"  squeue %b GPU jobs   : {sum(1 for l in lines if '|' in l and l.split('|')[3] not in ('N/A','(null)',''))}")

        if gpu_from_sacct > 0:
            print()
            print(BOLD(f"  Recalculated cost for {min(gpu_from_sacct,5)} sample live GPU jobs:"))
            gpu_tres = [l for l in sacct_r.stdout.decode().splitlines()
                        if "gres/gpu" in l and "." not in l.split("|")[0]][:5]
            for entry in gpu_tres:
                jid, tres = entry.split("|", 1)
                # Quick TRES parse
                gpu_count, gpu_model = 0, ""
                for part in tres.lower().split(","):
                    if "gres/gpu" in part and "=" in part:
                        key, val = part.rsplit("=", 1)
                        try: gpu_count = int(val)
                        except: gpu_count = 1
                        gpu_model = key.split(":",1)[1] if ":" in key else ""
                        break
                inst = find_best_instance(AWS_INSTANCES, 1, 0, gpu_count, gpu_model)
                print(f"     Job {jid.strip():<14}  gpu={gpu_count}×{gpu_model or '?'}  "
                      f"→ AWS {inst['name']}  ${inst['price']:.3f}/hr")
    except FileNotFoundError:
        print(YELLOW("  ⚠  squeue/sacct not found. Set --slurm-bin to your Slurm bin path."))
    except subprocess.TimeoutExpired:
        print(YELLOW("  ⚠  Slurm commands timed out."))

# ─────────────────────────────────────────────────────────────────────────────
# 9. ANOMALIES
# ─────────────────────────────────────────────────────────────────────────────
def cmd_anomalies(args):
    section("ANOMALY DETECTION")
    conn  = open_db(args.db)
    where, params = build_where(args)
    issues = 0

    # a) GPU jobs with $0 cost
    rows = conn.execute(f"""
        SELECT job_id, cluster, username, gpu_count, gpu_model,
               req_cpus, ROUND(req_mem_mb/1024.0,0) AS mem_gb,
               elapsed_min, aws_instance, aws_total
        FROM jobs {where}
          {'AND' if where else 'WHERE'} gpu_count > 0 AND aws_total = 0
        LIMIT ?
    """, params + [args.top]).fetchall()
    if rows:
        issues += len(rows)
        print(YELLOW(f"  ⚠  GPU jobs with $0.00 AWS cost  ({len(rows)} shown):"))
        print_table(
            ["Job ID", "Cluster", "User", "GPUs", "Model", "CPUs", "Mem GB", "Min", "Instance", "AWS $"],
            [(r["job_id"][:16], r["cluster"], r["username"][:12],
              r["gpu_count"], r["gpu_model"] or "—", r["req_cpus"],
              f"{r['mem_gb']:.0f}", r["elapsed_min"], r["aws_instance"], f"${r['aws_total']:.4f}")
             for r in rows]
        )
        print()

    # b) Jobs with anomalously high cost (>3σ above mean)
    stats = conn.execute(f"""
        SELECT AVG(aws_total) AS mean,
               AVG(aws_total*aws_total) - AVG(aws_total)*AVG(aws_total) AS variance
        FROM jobs {where}
          {'AND' if where else 'WHERE'} aws_total > 0
    """, params).fetchone()
    if stats["mean"] and stats["variance"] and stats["variance"] > 0:
        sigma = math.sqrt(stats["variance"])
        threshold = stats["mean"] + 3 * sigma
        outliers = conn.execute(f"""
            SELECT job_id, cluster, username, gpu_count, gpu_model,
                   req_cpus, ROUND(req_mem_mb/1024.0,0) AS mem_gb,
                   elapsed_min, time_limit_min,
                   aws_instance, aws_total
            FROM jobs {where}
              {'AND' if where else 'WHERE'} aws_total > ?
            ORDER BY aws_total DESC LIMIT ?
        """, params + [threshold, args.top]).fetchall()
        if outliers:
            issues += len(outliers)
            print(YELLOW(f"  ⚠  High-cost outliers (>3σ above mean, threshold {fmt_money(threshold)}):"))
            print_table(
                ["Job ID", "Cluster", "User", "GPUs", "Model", "CPUs", "Elapsed min", "Time Limit min", "Instance", "AWS $"],
                [(r["job_id"][:16], r["cluster"], r["username"][:12],
                  r["gpu_count"], r["gpu_model"] or "—", r["req_cpus"],
                  r["elapsed_min"], r["time_limit_min"],
                  r["aws_instance"][:22], fmt_money(r["aws_total"]))
                 for r in outliers]
            )
            print()

    # c) Jobs with elapsed_min=0 (never ran or bad sacct data)
    zero_elapsed = conn.execute(f"""
        SELECT COUNT(*) AS cnt FROM jobs {where}
          {'AND' if where else 'WHERE'} elapsed_min = 0
    """, params).fetchone()["cnt"]
    if zero_elapsed:
        issues += zero_elapsed
        print(YELLOW(f"  ⚠  {zero_elapsed:,} jobs with elapsed_min=0 (bad sacct data or never ran)"))

    # d) Jobs with end_time < start_time
    bad_times = conn.execute(f"""
        SELECT COUNT(*) AS cnt FROM jobs {where}
          {'AND' if where else 'WHERE'} end_time > 0 AND end_time < start_time
    """, params).fetchone()["cnt"]
    if bad_times:
        issues += bad_times
        print(RED(f"  ✗  {bad_times:,} jobs where end_time < start_time (corrupted timestamps)"))

    # e) Jobs with suspiciously large memory (>2TB)
    big_mem = conn.execute(f"""
        SELECT job_id, cluster, username, req_cpus,
               ROUND(req_mem_mb/1024.0,0) AS mem_gb, elapsed_min, aws_total
        FROM jobs {where}
          {'AND' if where else 'WHERE'} req_mem_mb > 2097152
        ORDER BY req_mem_mb DESC LIMIT ?
    """, params + [args.top]).fetchall()
    if big_mem:
        issues += len(big_mem)
        print(YELLOW(f"  ⚠  Jobs requesting >2 TB RAM ({len(big_mem)} shown) — verify units are correct:"))
        print_table(
            ["Job ID", "Cluster", "User", "CPUs", "Mem GB", "Elapsed min", "AWS $"],
            [(r["job_id"][:16], r["cluster"], r["username"][:12],
              r["req_cpus"], f"{r['mem_gb']:.0f}", r["elapsed_min"], fmt_money(r["aws_total"]))
             for r in big_mem]
        )
        print()

    # f) Duplicate job IDs within a cluster
    dupes = conn.execute(f"""
        SELECT job_id, cluster, COUNT(*) AS cnt
        FROM jobs {where}
        GROUP BY job_id, cluster HAVING cnt > 1
        LIMIT 10
    """, params).fetchall()
    if dupes:
        issues += len(dupes)
        print(RED(f"  ✗  {len(dupes)} duplicate (job_id, cluster) pairs found — DB integrity issue"))
        print_table(["Job ID", "Cluster", "Count"], [(r["job_id"], r["cluster"], r["cnt"]) for r in dupes])

    # g) Stale GPU model entries (model stored but not in GPU_MODEL_MAP)
    all_models = conn.execute(f"""
        SELECT DISTINCT gpu_model FROM jobs {where}
          {'AND' if where else 'WHERE'} gpu_count > 0 AND gpu_model != ''
    """, params).fetchall()
    unknown_models = [r["gpu_model"] for r in all_models if r["gpu_model"].lower() not in GPU_MODEL_MAP]
    if unknown_models:
        issues += len(unknown_models)
        print(YELLOW(f"  ⚠  GPU models in DB not in GPU_MODEL_MAP (may use wrong cloud instance):"))
        for m in unknown_models:
            print(f"       '{m}'  →  add to GPU_MODEL_MAP in hpc-cost-comparator/load_pricelist.py")

    if issues == 0:
        print(GREEN("  ✓  No anomalies detected"))
    else:
        print()
        print(YELLOW(f"  Total anomaly count: {issues}"))
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
COMMANDS = {
    "summary":      cmd_summary,
    "users":        cmd_users,
    "cluster":      cmd_cluster,
    "cloud":        cmd_cloud,
    "gpu":          cmd_gpu,
    "cpu":          cmd_cpu,
    "pricing":      cmd_pricing,
    "recalc-check": cmd_recalc_check,
    "live":         cmd_live,
    "anomalies":    cmd_anomalies,
}

def main():
    p = argparse.ArgumentParser(
        description="Shovly data validation and audit tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="  Example:\n"
               "    python3 validate.py all --start 2025-01-01\n"
               "    python3 validate.py gpu --cluster slurm --start 2025-06-01\n"
               "    python3 validate.py cpu --cluster slurm --start 2025-06-01\n"
               "    python3 validate.py live --api http://red2.moffitt.org:8000 "
               "--slurm-bin /cm/shared/apps/slurm/current/bin\n"
               "    python3 validate.py recalc-check --start 2025-01-01 --top 50\n"
    )
    p.add_argument("command", choices=list(COMMANDS) + ["all"],
                   help="Validation subcommand to run")
    p.add_argument("--db",        default="data/historical.db",
                   help="Path to historical SQLite database")
    p.add_argument("--start",     metavar="YYYY-MM-DD",
                   help="Include jobs with start_time >= this date")
    p.add_argument("--end",       metavar="YYYY-MM-DD",
                   help="Include jobs with start_time <  this date + 1 day")
    p.add_argument("--cluster",   default="all",
                   help="Limit to a single cluster name (default: all)")
    p.add_argument("--top",       type=int, default=20,
                   help="Number of rows in ranked tables (default: 20)")
    p.add_argument("--api",       default="http://localhost:8000",
                   help="Dashboard base URL for live subcommand")
    p.add_argument("--slurm-bin", dest="slurm_bin", default="",
                   help="Slurm bin directory for live subcommand")

    args = p.parse_args()

    print(BOLD(f"\nShovly Validator  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))
    print(f"DB: {args.db}  |  cluster: {args.cluster}  |  "
          f"range: {args.start or 'all'} → {args.end or 'now'}")

    offline = ["summary", "users", "cluster", "cloud", "gpu", "cpu", "pricing", "recalc-check", "anomalies"]
    if args.command == "all":
        for cmd in offline:
            COMMANDS[cmd](args)
    else:
        COMMANDS[args.command](args)

    print()

if __name__ == "__main__":
    main()
