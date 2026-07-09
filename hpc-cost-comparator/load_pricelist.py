#!/usr/bin/env python3
"""
load_pricelist.py — Load AWS and Azure instance catalogs from CSV price list files.

Called by app.py, import_history.py, and validate.py to replace hardcoded
instance lists.  Drop updated CSV files into the data/ directory to refresh
prices without touching application code.

CSV files (both use column names that say "MB" but store values in GB):
  data/AWS-pricelist.csv   — UTF-8 BOM, columns:
      InstanceType, InstanceFamily, ProcessorVCPUCount, MemorySizeInMB*,
      ProcessorArchitecture, PricePerHour, GPUCount, GPUName, GPUMemoryInMB*,
      ProcessorDefaultCores
  data/Azure-pricelist.csv — UTF-8, columns:
      name, numberOfCores, memoryInMB*, cpuArchitecture, gpUs, gpuType,
      gpuRam*, linuxPrice

  * These fields are named "MB" but contain GB values.
"""

import csv
import logging
import os

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_AWS_CSV   = os.path.join(_DATA_DIR, "AWS-pricelist.csv")
_AZURE_CSV = os.path.join(_DATA_DIR, "Azure-pricelist.csv")

# ---------------------------------------------------------------------------
# GPU model normalisation map
# Keys are lowercase strings as they appear in:
#   - Slurm GRES names (from squeue / sacct / node_gpu_map.csv)
#   - AWS CSV GPUName column
#   - Azure CSV gpuType column (verbose English strings)
# Values are canonical names used throughout the codebase and for
# find_best_instance() matching.
# ---------------------------------------------------------------------------
GPU_MODEL_MAP = {
    # ── Slurm GRES names (lowercase, as reported in GRES/TRES fields) ─────────
    "h100":         "H100",
    "h200":         "H200",
    "a100":         "A100",
    "a30":          "A10G",   # A30 → A10G (closest cloud equivalent)
    "a10g":         "A10G",
    "a10":          "A10G",
    "v100":         "V100",
    "t4":           "T4",
    "l40":          "L40S",
    "l40s":         "L40S",
    "nvidia_l40s":  "L40S",   # as stored in node_gpu_map.csv by some clusters
    "l4":           "L4",
    "b200":         "B200",
    "b300":         "B300",
    # No direct cloud equivalent — cost lookup falls back to cheapest GPU instance
    "gb10":         "GB10",   # NVIDIA Grace Blackwell 10 (not in AWS/Azure yet)
    "rtx6000":      "RTX6000", # Quadro RTX 6000 — no matching cloud instance type

    # ── AWS CSV GPUName strings ────────────────────────────────────────────────
    # (These are already fairly normalised; included so _normalize_gpu works
    #  regardless of which CSV column provides the raw string.)
    "a10g":         "A10G",
    "l40s":         "L40S",
    "rtx pro server 6000": "RTXPRO6000",
    "radeon pro v520":     "",   # AMD GPU — no direct cloud equivalent

    # ── Azure CSV gpuType strings (verbose English format) ────────────────────
    "nvidia h100":            "H100",
    "nvidia h200":            "H200",
    "nvidia a100":            "A100",
    "nvidia a100 (80gb)":     "A100",
    "nvidia a100 (40gb)":     "A100",
    "nvidia a10":             "A10G",
    "nvidia a10 (24gb)":      "A10G",
    "nvidia a10g":            "A10G",
    "nvidia tesla v100":      "V100",
    "nvidia t4":              "T4",
    "nvidia l40s":            "L40S",
    "nvidia l40":             "L40S",
    "nvidia l4":              "L4",
    "nvidia b200":            "B200",
    # Old/specialty GPUs with no cloud equivalent — fall back to cheapest instance
    "nvidia tesla m60 (16gb)":     "",
    "amd alveo u250 fpga (64gb)":  "",  # FPGA, not a GPU
    "amd radeon instinct mi25":    "",
}


def _normalize_gpu(raw: str) -> str:
    """Return a canonical GPU model name from a raw CSV / GRES GPU string."""
    if not raw or not raw.strip():
        return ""
    key = raw.strip().lower()
    return GPU_MODEL_MAP.get(key, raw.strip())


def load_aws_catalog(path: str = _AWS_CSV) -> list:
    """
    Parse AWS-pricelist.csv and return a list of instance dicts.

    Dict keys: name, vcpus, mem_gb, gpu_count, gpu_model, price.
    Rows with missing or zero price are skipped.

    Note: the MemorySizeInMB column contains values in GB despite its name.
    """
    catalog = []
    skipped_na = 0
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    raw_price = (row.get("PricePerHour") or "").strip()
                    if raw_price.lower() in ("", "n/a", "na", "0"):
                        skipped_na += 1
                        continue
                    price = float(raw_price)
                    if price <= 0:
                        continue
                    catalog.append({
                        "name":      row["InstanceType"].strip(),
                        "vcpus":     int(row["ProcessorVCPUCount"]),
                        "mem_gb":    float(row["MemorySizeInMB"]),  # values are in GB
                        "gpu_count": int(row.get("GPUCount") or 0),
                        "gpu_model": _normalize_gpu(row.get("GPUName", "")),
                        "price":     price,
                    })
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        logger.warning("AWS price list not found: %s — using empty catalog.", path)
    if skipped_na:
        logger.info("AWS catalog: %d instances loaded, %d skipped (n/a price) from %s",
                    len(catalog), skipped_na, path)
    else:
        logger.info("AWS catalog: %d instances loaded from %s", len(catalog), path)
    return catalog


def load_azure_catalog(path: str = _AZURE_CSV) -> list:
    """
    Parse Azure-pricelist.csv and return a list of instance dicts.

    Dict keys: name, vcpus, mem_gb, gpu_count, gpu_model, price.
    Rows with missing or zero price are skipped.

    Note: the memoryInMB column contains values in GB despite its name.
    """
    catalog = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    price = float(row.get("linuxPrice") or 0)
                    if price <= 0:
                        continue
                    catalog.append({
                        "name":      row["name"].strip(),
                        "vcpus":     int(row["numberOfCores"]),
                        "mem_gb":    float(row["memoryInMB"]),   # values are in GB
                        "gpu_count": int(row.get("gpUs") or 0),
                        "gpu_model": _normalize_gpu(row.get("gpuType", "")),
                        "price":     price,
                    })
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        logger.warning("Azure price list not found: %s — using empty catalog.", path)
    logger.info("Azure catalog: %d instances loaded from %s", len(catalog), path)
    return catalog


def load_catalogs(aws_path: str = _AWS_CSV, azure_path: str = _AZURE_CSV):
    """Load both catalogs and return (aws_instances, azure_instances)."""
    return load_aws_catalog(aws_path), load_azure_catalog(azure_path)
