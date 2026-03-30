#!/usr/bin/env python3
"""
Unified FHO Evaluation Pipeline
Downloads and processes FHO, LSR, and WWA data into GeoPackage files.

Usage:
    python pipeline.py                          # Full run, all years 2022-2025
    python pipeline.py --years 2024 2025        # Specific years only
    python pipeline.py --only fho               # Only process FHO dataset
    python pipeline.py --update                 # Incremental update from last state
    python pipeline.py --fho-source /path/to/zips  # Use local FHO zip directory (instead of NWC)
"""

import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from glob import glob
import fiona
import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import urllib3
try:
    from shapely.ops import unary_union as _unary_union
except ImportError:
    _unary_union = None
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Detect non-TTY environments (Spyder IPython console, redirected output, etc.)
# tqdm progress bars don't render correctly there — we use periodic print() instead.
_NO_TTY = not sys.stdout.isatty()

# Suppress SSL warnings (NWC server uses self-signed certs)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_YEARS = list(range(2022, date.today().year + 1))
CRS_TARGET = "EPSG:5070"

FHO_HOST = "https://ops.nwc.nws.noaa.gov"
LSR_API = "https://mesonet.agron.iastate.edu/geojson/lsr.php"
WWA_PICKUP = "https://mesonet.agron.iastate.edu/pickup/wwa"

STATE_FILE = "pipeline_state.json"
MAX_WORKERS = 6
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, doubled each retry
LSR_CHUNK_DAYS = 90

# FHO source mode — set by CLI args ("nwc" or a local directory path)
_FHO_SOURCE = "nwc"


def _set_max_workers(n):
    global MAX_WORKERS
    MAX_WORKERS = n


_gpkg_write_lock = threading.Lock()


def _write_layer(gdf, output_path, layer_name):
    """Write a GeoDataFrame to a GeoPackage, replacing the layer if it already exists.

    Serialized with a lock since GPKG/SQLite is not safe for concurrent writes.
    """
    with _gpkg_write_lock:
        if os.path.exists(output_path):
            try:
                if layer_name in fiona.listlayers(output_path):
                    fiona.remove(output_path, layer=layer_name)
            except Exception as exc:
                log.debug("Could not check/remove existing layer %s: %s", layer_name, exc)
        gdf.to_file(output_path, layer=layer_name, driver="GPKG")

# IEM LSR field rename mapping
LSR_FIELD_MAP = {
    "typetext": "EVENT",
    "remark": "REMARKS",
    "city": "CITY",
    "state": "STATE",
    "source": "SOURCE",
    "valid": "VALID",
    "lat": "LAT",
    "lon": "LON",
    "wfo": "WFO",
    "type": "TYPECODE",
    "county": "COUNTY",
    "qualifier": "QUALIFIER",
    "magnitude": "MAG",
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
_thread_local = threading.local()


def _get_session():
    """Return a per-thread requests.Session with connection pooling sized to match workers."""
    if not hasattr(_thread_local, "session"):
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS * 2,
            max_retries=0,  # we handle retries ourselves
        )
        s = requests.Session()
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return _thread_local.session


class _NotFound:
    """Sentinel returned by http_get for 404 responses (vs None for real failures)."""
    pass

NOT_FOUND = _NotFound()


def http_get(url, *, stream=False, timeout=60):
    """GET with retry + exponential backoff.  Returns Response, NOT_FOUND for 404, or None on failure."""
    session = _get_session()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, verify=False, stream=stream, timeout=timeout)
            if r.status_code == 404:
                return NOT_FOUND
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                log.warning("Failed after %d attempts: %s – %s", MAX_RETRIES, url, exc)
                return None
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.debug("Retry %d/%d in %ds: %s", attempt, MAX_RETRIES, wait, url)
            time.sleep(wait)
    return None


def http_download_to_file(url, dest_path, *, timeout=300, desc=None):
    """Stream a large file to disk with a tqdm progress bar. Returns True on success."""
    session = _get_session()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, verify=False, stream=True, timeout=timeout)
            if r.status_code == 404:
                return False
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            with open(dest_path, "wb") as f, tqdm(
                total=total, unit="B", unit_scale=True, unit_divisor=1024,
                desc=desc or os.path.basename(dest_path),
                disable=total == 0,
            ) as pbar:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    f.write(chunk)
                    pbar.update(len(chunk))
            return True
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                log.warning("Download failed after %d attempts: %s – %s", MAX_RETRIES, url, exc)
                return False
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.debug("Download retry %d/%d in %ds: %s", attempt, MAX_RETRIES, wait, url)
            time.sleep(wait)
    return False


# ---------------------------------------------------------------------------
# State tracking (thread-safe)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()


def load_state(path=STATE_FILE):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"fho_last_date": {}, "lsr_last_date": None, "wwa_years_done": []}


def _atomic_write_json(data, path):
    """Write JSON atomically via temp file + rename to prevent corruption on crash."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def save_state(state, path=STATE_FILE):
    with _state_lock:
        _atomic_write_json(state, path)


def _update_state(state, updater):
    """Thread-safe state read-modify-write. updater(state) mutates state in place."""
    with _state_lock:
        updater(state)
        _atomic_write_json(state, STATE_FILE)


def _gpkg_has_layer(gpkg_path, layer_name):
    """Return True if layer_name exists in the GeoPackage."""
    if not os.path.exists(gpkg_path):
        return False
    try:
        return layer_name in fiona.listlayers(gpkg_path)
    except Exception:
        return False


def _state_has_data(state):
    """Return True if pipeline_state.json contains any meaningful progress."""
    return (bool(state.get("fho_last_date")) or
            bool(state.get("lsr_last_date")) or
            bool(state.get("wwa_years_done")))


# ===================================================================
# FHO Processing
# ===================================================================

def extract_date_from_filename(path):
    # Handles both fho_YYYYMMDD_am|pm_final.zip and fho_national_YYYYMMDD_am|pm_final.zip
    m = re.search(r"fho_(?:[a-z]+_)?(\d{8})_(am|pm)", path)
    return m.group(1) if m else None


def parse_valid_range(issuance_date_str, period, issuance_time="am"):
    dt = datetime.strptime(issuance_date_str, "%Y%m%d")
    if issuance_time.lower() == "am":
        dt = dt.replace(hour=12)
    else:
        dt = dt.replace(hour=21)

    ranges = {
        "1-3": (dt, dt + timedelta(days=3)),
        "4-7": (dt + timedelta(days=3), dt + timedelta(days=7)),
        "1-7": (dt, dt + timedelta(days=7)),
    }
    return ranges.get(period, (dt, dt))


def forecast_days_list(period):
    return {
        "1-3": [1, 2, 3],
        "4-7": [4, 5, 6, 7],
        "1-7": [1, 2, 3, 4, 5, 6, 7],
    }.get(period, [])


def forecast_category_score(level):
    return {"Limited": 1, "Considerable": 2, "Catastrophic": 3}.get(level, 0)


def parse_category(category_str):
    """Parse CATEGORY string into (period, level).

    Edge cases (EC-1 through EC-6 in spec):
      - "See Text" → period 1-3, level Limited
      - "Through Day 3" → 1-3
      - "Through Day 7" → 1-7
      - "Through Day 4/5/6" → Unknown period
      - Explicit 1-3 / 4-7 / 1-7 tokens take priority
    """
    cleaned = category_str.replace("\u2013", "-").replace("\u2014", "-")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # --- period ---
    period = "Unknown"
    explicit = re.search(r"(1-3|4-7|1-7)", cleaned)
    if explicit:
        period = explicit.group(1)
    else:
        through = re.search(r"Through Day (\d)", cleaned, re.IGNORECASE)
        if through:
            day = through.group(1)
            if day == "3":
                period = "1-3"
            elif day == "7":
                period = "1-7"
            # Day 4/5/6 → stays Unknown
        elif "See Text" in cleaned:
            period = "1-3"

    # --- level ---
    lm = re.search(r"(Limited|Considerable|Catastrophic)", cleaned, re.IGNORECASE)
    if lm:
        level = lm.group(1).capitalize()
    elif "See Text" in cleaned:
        level = "Limited"
    else:
        level = "Unknown"

    return period, level


def _detect_period_from_filename(shp_name):
    """If filename contains 'Days1-3' etc., return that period string."""
    m = re.search(r"Days?(1-3|4-7|1-7)", shp_name, re.IGNORECASE)
    return m.group(1) if m else None


def process_fho_zip_bytes(zip_bytes, zip_name, issuance_time):
    """Process an in-memory FHO zip.  Returns list of dicts (rows)."""
    issuance_date = extract_date_from_filename(zip_name)
    if not issuance_date:
        log.warning("Cannot extract date from %s", zip_name)
        return []

    rows = []
    tmp = tempfile.mkdtemp(prefix="fho_")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            shp_names = [n for n in zf.namelist() if n.endswith(".shp")]
            for shp_file in shp_names:
                base = os.path.splitext(shp_file)[0]
                exts = [".shp", ".shx", ".dbf", ".prj", ".cpg"]
                for ext in exts:
                    try:
                        zf.extract(base + ext, path=tmp)
                    except KeyError:
                        pass

                shp_path = os.path.join(tmp, base + ".shp")
                if not os.path.exists(shp_path):
                    continue

                try:
                    gdf = gpd.read_file(shp_path)
                except Exception as exc:
                    log.warning("Error reading %s in %s: %s", shp_file, zip_name, exc)
                    continue

                gdf = gdf.to_crs(CRS_TARGET)
                gdf = gdf[gdf.geometry.notnull() & gdf.geometry.is_valid]
                gdf["geometry"] = gdf["geometry"].buffer(0)
                if gdf.empty:
                    continue

                gdf["area_sqkm"] = gdf.geometry.area / 1e6
                gdf["centroid"] = gdf.geometry.centroid

                # EC-7: period-specific filename overrides CATEGORY
                filename_period = _detect_period_from_filename(shp_file)

                for i, row in gdf.iterrows():
                    cat_text = row.get("CATEGORY", "") or ""

                    # 'no area' = placeholder issuance with no actual flood hazard polygon — skip
                    if str(cat_text).strip().lower() == "no area":
                        log.debug("Skipping 'no area' row in %s", zip_name)
                        continue

                    period, level = parse_category(str(cat_text))

                    # Filename wins over CATEGORY (EC-8)
                    if filename_period:
                        period = filename_period

                    if period == "Unknown":
                        log.debug("Unknown period from CATEGORY '%s' in %s", cat_text, zip_name)

                    vs, ve = parse_valid_range(issuance_date, period, issuance_time)
                    c = row["centroid"]
                    rows.append({
                        "polygon_id": f"{issuance_date}_{issuance_time}_{period}_{i}",
                        "issuance_date": issuance_date,
                        "year": int(issuance_date[:4]),
                        "month": int(issuance_date[4:6]),
                        "day": int(issuance_date[6:]),
                        "issuance_time": issuance_time.upper(),
                        "forecast_period": period,
                        "forecast_period_days": str(forecast_days_list(period)),
                        "impact_level": level,
                        "forecast_category_score": forecast_category_score(level),
                        "valid_start": vs,
                        "valid_end": ve,
                        "area_sqkm": row["area_sqkm"],
                        "centroid_x": c.x,
                        "centroid_y": c.y,
                        "source_filename": os.path.basename(shp_file),
                        "source_category": cat_text,
                        "hit_type": None,
                        "geometry": row.geometry,
                    })
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return rows


def create_limited_inclusive_layer(gdf):
    """Create Limited_merged rows by unioning Limited+Considerable+Catastrophic per group."""
    merged = []
    valid_levels = {"Limited", "Considerable", "Catastrophic"}
    grouped = gdf.groupby(["issuance_date", "issuance_time", "forecast_period"])

    for (dt, tm, per), grp in grouped:
        subset = grp[grp["impact_level"].isin(valid_levels)]
        if subset.empty:
            continue
        geom_series = subset.geometry
        if hasattr(geom_series, 'union_all'):
            union_geom = geom_series.union_all()
        else:
            union_geom = _unary_union(geom_series)
        if union_geom.is_empty:
            continue
        vs, ve = parse_valid_range(dt, per, tm)
        merged.append({
            "polygon_id": f"{dt}_{tm}_{per}_LimitedMerged",
            "issuance_date": dt,
            "year": int(dt[:4]),
            "month": int(dt[4:6]),
            "day": int(dt[6:]),
            "issuance_time": tm.upper(),
            "forecast_period": per,
            "forecast_period_days": str(forecast_days_list(per)),
            "impact_level": "Limited_merged",
            "forecast_category_score": 1,
            "valid_start": vs,
            "valid_end": ve,
            "area_sqkm": union_geom.area / 1e6,
            "centroid_x": union_geom.centroid.x,
            "centroid_y": union_geom.centroid.y,
            "source_filename": "Merged",
            "hit_type": None,
            "area_bin": None,
            "geometry": union_geom,
        })

    if not merged:
        return gpd.GeoDataFrame(columns=list(gdf.columns) + ["area_bin"], geometry="geometry", crs=gdf.crs)
    return gpd.GeoDataFrame(merged, crs=gdf.crs)


def _download_single_fho(url):
    """Download a single FHO zip from NWC. Returns (filename, bytes, status).
    status is '404' for expected missing files, 'failed' for errors, 'ok' on success.
    """
    fname = url.rsplit("/", 1)[-1]
    resp = http_get(url)
    if isinstance(resp, _NotFound):
        return fname, None, "404"
    if resp is None:
        return fname, None, "failed"
    return fname, resp.content, "ok"


def _load_single_fho_local(zip_path):
    """Load a single FHO zip from local disk. Returns (filename, bytes) or (filename, None)."""
    fname = os.path.basename(zip_path)
    try:
        with open(zip_path, "rb") as f:
            return fname, f.read()
    except Exception as exc:
        log.warning("Error reading local file %s: %s", zip_path, exc)
        return fname, None


# ---------------------------------------------------------------------------
# Summary report utilities
# ---------------------------------------------------------------------------

def _calendar_days_in_range(start_d, end_d):
    """Count calendar days in [start_d, end_d] inclusive."""
    if start_d > end_d:
        return 0
    return (end_d - start_d).days + 1


def _format_size(n_bytes):
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TB"


def _format_elapsed(seconds):
    """Human-readable elapsed time (e.g. '2m 34s')."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


# ---------------------------------------------------------------------------
# Monthly summary reporters (print to stdout so they stand out from log lines)
# ---------------------------------------------------------------------------

def _fho_monthly_report(gdf, year, mode, proc_start, proc_end):
    """Print per-month FHO issuance coverage, polygon counts, and impact breakdown."""
    if gdf.empty:
        return
    base = gdf[gdf["impact_level"] != "Limited_merged"]
    if base.empty:
        return

    import calendar as _cal
    today = date.today()
    max_month = 12 if year < today.year else today.month

    dates_by_mo = base.groupby("month")["issuance_date"].nunique().to_dict()
    polys_by_mo = base.groupby("month").size().to_dict()

    # Coverage rate: actual issued days vs calendar days in range
    total_expected = _calendar_days_in_range(proc_start, proc_end)
    total_issued = base["issuance_date"].nunique()
    cov_pct = (total_issued / total_expected * 100) if total_expected else 0

    # Impact level distribution (exclude Limited_merged, Unknown)
    level_counts = base["impact_level"].value_counts().to_dict()

    # Peak month by polygon count
    peak_mo = max(polys_by_mo, key=polys_by_mo.get) if polys_by_mo else None

    W = 56
    sep = "  " + "─" * W
    print(f"\n{sep}")
    print(f"  FHO {year} {mode.upper()}  ─  Monthly Summary")
    print(sep)
    print(f"  {'Month':<10} {'Days Exp':>9} {'Issued':>8} {'Cov%':>6} {'Polygons':>10}")
    print(f"  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*5}  {'─'*10}")
    for mo in range(1, max_month + 1):
        mo_start = date(year, mo, 1)
        mo_end   = date(year, mo, _cal.monthrange(year, mo)[1])
        eff_start = max(mo_start, proc_start)
        eff_end   = min(mo_end,   proc_end)
        exp = _calendar_days_in_range(eff_start, eff_end) if eff_start <= eff_end else 0
        n_dates = dates_by_mo.get(mo, 0)
        n_polys = polys_by_mo.get(mo, 0)
        pct = f"{n_dates/exp*100:.0f}%" if exp else "─"
        flag = "  << NO DATA" if n_dates == 0 and exp > 0 else ""
        peak_marker = "  ◀ peak" if mo == peak_mo else ""
        print(f"  {year}-{mo:02d}      {exp:>9}  {n_dates:>7}  {pct:>5}  {n_polys:>10}{flag}{peak_marker}")
    print(f"  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*5}  {'─'*10}")
    print(f"  {'TOTAL':<10} {total_expected:>9}  {total_issued:>7}  {cov_pct:>4.0f}%  {len(base):>10}")
    print(sep)
    print(f"  Impact levels ─ " + "  ".join(
        f"{lvl}: {level_counts.get(lvl, 0):,}"
        for lvl in ("Limited", "Considerable", "Catastrophic", "Unknown")
        if level_counts.get(lvl, 0) > 0
    ))

    # Archive date range — helps explain NO DATA months from incomplete local archives
    min_date = base["issuance_date"].min()
    max_date = base["issuance_date"].max()
    min_fmt = f"{min_date[:4]}-{min_date[4:6]}-{min_date[6:]}" if min_date else "?"
    max_fmt = f"{max_date[:4]}-{max_date[4:6]}-{max_date[6:]}" if max_date else "?"
    print(f"  Archive range  ─ {min_fmt} → {max_fmt}")

    # Unknown CATEGORY breakdown — list raw strings that couldn't be parsed
    if level_counts.get("Unknown", 0) > 0 and "source_category" in base.columns:
        unk = base[base["impact_level"] == "Unknown"]
        cat_groups = (
            unk.groupby("source_category")["issuance_date"]
            .agg(count="count", earliest="min")
            .sort_values("count", ascending=False)
        )
        print(f"  Unknown CATEGORY strings ({len(unk):,} rows across {len(cat_groups)} unique values):")
        for cat_val, row in cat_groups.iterrows():
            d = row["earliest"]
            d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else "?"
            display = repr(cat_val) if cat_val else '""'
            print(f"    {row['count']:>5}×  {display:<40}  earliest: {d_fmt}")

    print(f"{sep}\n")


def _lsr_monthly_report(gdf, years):
    """Print per year-month LSR counts with event-type split, date range, and peak month."""
    if gdf.empty or "VALID" not in gdf.columns:
        return
    tmp = gdf[gdf["VALID"].notna()].copy()
    if tmp.empty:
        return
    tmp["_ym"] = pd.to_datetime(tmp["VALID"]).dt.to_period("M")
    counts = tmp.groupby("_ym").size()

    # Event-type split (Flood vs Flash Flood)
    type_counts = {}
    if "EVENT" in gdf.columns:
        type_counts = gdf["EVENT"].str.title().value_counts().to_dict()

    # Actual date range
    valid_min = pd.to_datetime(gdf["VALID"]).min()
    valid_max = pd.to_datetime(gdf["VALID"]).max()

    # Peak month
    peak_p = counts.idxmax() if not counts.empty else None

    today = date.today()
    W = 46
    sep = "  " + "─" * W
    print(f"\n{sep}")
    print(f"  LSR  ─  Monthly Flood / Flash Flood Event Counts")
    print(sep)
    print(f"  Date range in file: {valid_min.strftime('%Y-%m-%d')}  →  {valid_max.strftime('%Y-%m-%d')}")
    if type_counts:
        split_str = "  ".join(f"{k}: {v:,}" for k, v in sorted(type_counts.items()))
        print(f"  Event types  ─  {split_str}")
    print(sep)
    print(f"  {'Year-Month':<12} {'Events':>8}")
    print(f"  {'─'*12}  {'─'*8}")
    for yr in sorted(years):
        max_month = 12 if yr < today.year else today.month
        for mo in range(1, max_month + 1):
            p = pd.Period(f"{yr}-{mo:02d}", freq="M")
            n = int(counts.get(p, 0))
            flag = "  << NO DATA" if n == 0 else ""
            peak_marker = "  ◀ peak" if p == peak_p else ""
            print(f"  {yr}-{mo:02d}        {n:>8}{flag}{peak_marker}")
    print(f"  {'─'*12}  {'─'*8}")
    print(f"  {'TOTAL':<12} {len(gdf):>8}")
    print(f"{sep}\n")


def _wwa_monthly_report(gdf, year):
    """Print per-month WWA warning counts with FF/FL split and DAMAGTAG breakdown."""
    if gdf.empty or "ISSUED" not in gdf.columns:
        return
    tmp = gdf.copy()
    tmp["_month"] = pd.to_datetime(tmp["ISSUED"], errors="coerce").dt.month
    tmp["_phenom"] = gdf.get("PHENOM", pd.Series("", index=gdf.index))
    tmp = tmp[tmp["_month"].notna()]
    if tmp.empty:
        return

    by_mo_ph = tmp.groupby(["_month", "_phenom"]).size().unstack(fill_value=0)
    total_by_mo = tmp.groupby("_month").size()

    # DAMAGTAG breakdown
    damagtag_counts = {}
    if "DAMAGTAG" in gdf.columns:
        raw = gdf["DAMAGTAG"].fillna("").str.upper()
        damagtag_counts = raw.value_counts().to_dict()

    today = date.today()
    max_month = 12 if year < today.year else today.month
    peak_mo = int(total_by_mo.idxmax()) if not total_by_mo.empty else None

    W = 60
    sep = "  " + "─" * W
    print(f"\n{sep}")
    print(f"  WWA {year}  ─  Monthly Warning Counts")
    print(sep)
    if damagtag_counts:
        consid  = damagtag_counts.get("CONSIDERABLE", 0)
        catast  = damagtag_counts.get("CATASTROPHIC", 0)
        untagged = sum(v for k, v in damagtag_counts.items()
                       if k not in ("CONSIDERABLE", "CATASTROPHIC"))
        print(f"  DAMAGTAG breakdown ─  Considerable: {consid:,}  "
              f"Catastrophic: {catast:,}  Untagged: {untagged:,}")
    print(sep)
    print(f"  {'Month':<10} {'FF (Flash)':>10} {'FL (River)':>10} {'Total':>8}")
    print(f"  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*8}")
    for mo in range(1, max_month + 1):
        row = by_mo_ph.loc[mo] if mo in by_mo_ph.index else pd.Series({"FF": 0, "FL": 0})
        ff = int(row.get("FF", 0))
        fl = int(row.get("FL", 0))
        total = ff + fl
        flag = "  << NO DATA" if total == 0 else ""
        peak_marker = "  ◀ peak" if mo == peak_mo else ""
        print(f"  {year}-{mo:02d}      {ff:>10}  {fl:>10}  {total:>8}{flag}{peak_marker}")
    total_ff = int(by_mo_ph.get("FF", pd.Series(0, dtype=int)).sum())
    total_fl = int(by_mo_ph.get("FL", pd.Series(0, dtype=int)).sum())
    print(f"  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*8}")
    print(f"  {'TOTAL':<10} {total_ff:>10}  {total_fl:>10}  {total_ff+total_fl:>8}")
    print(f"{sep}\n")


def process_fho(years, output_path, state, update_mode=False):
    """Download + process all FHO data → fho_all.gpkg"""
    counters = {"downloaded": 0, "skipped_404": 0, "failed": 0, "processed": 0,
                "failed_items": [], "year_counts": {}, "latest_record": None}

    # Update-mode resume summary
    if update_mode and state.get("fho_last_date"):
        print("  FHO update mode — resuming from:")
        for k, v in sorted(state["fho_last_date"].items()):
            resume_d = (datetime.strptime(v, "%Y%m%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            print(f"    {k}: last saved {v}  →  fetching from {resume_d}")

    for year in years:
        for mode in ("am", "pm"):
            log.info("FHO %d %s – preparing downloads", year, mode.upper())
            key = f"{year}_{mode}"

            # Determine date range
            start = date(year, 1, 1)
            end = min(date(year, 12, 31), date.today())
            if update_mode and key in state.get("fho_last_date", {}):
                layer = f"fho_{year}_{mode}"
                if not _gpkg_has_layer(output_path, layer):
                    log.warning("  State has %s but layer missing on disk — re-fetching from start", key)
                    state.get("fho_last_date", {}).pop(key, None)
                else:
                    resume = datetime.strptime(state["fho_last_date"][key], "%Y%m%d").date() + timedelta(days=1)
                    if resume > end:
                        log.info("  Already up to date for %s", key)
                        continue
                    start = resume

            all_rows = []
            last_good_date = None
            iter_failures = 0

            if _FHO_SOURCE == "nwc":
                # Build NWC URLs
                urls = []
                d = start
                while d <= end:
                    ds = d.strftime("%Y%m%d")
                    urls.append(f"{FHO_HOST}/products/{year}/final/FHO/shpzip/fho_{ds}_{mode}_final.zip")
                    d += timedelta(days=1)

                if not urls:
                    continue

                print(f"\n▶ FHO {year} {mode.upper()}  —  checking {len(urls)} dates"
                      f"  ({start} → {end})")
                _done = 0
                _milestone = max(1, len(urls) // 10)
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = {pool.submit(_download_single_fho, u): u for u in urls}
                    for fut in tqdm(as_completed(futures), total=len(futures),
                                    desc=f"FHO {year} {mode.upper()}", unit="zip",
                                    disable=_NO_TTY):
                        fname, data, status = fut.result()
                        _done += 1
                        if status == "404":
                            counters["skipped_404"] += 1
                        elif status == "failed":
                            counters["failed"] += 1
                            iter_failures += 1
                            d_str = extract_date_from_filename(fname)
                            counters["failed_items"].append(f"FHO {year} {mode.upper()} {d_str or fname}")
                        else:
                            counters["downloaded"] += 1
                            try:
                                rows = process_fho_zip_bytes(data, fname, mode)
                                all_rows.extend(rows)
                                d_str = extract_date_from_filename(fname)
                                if d_str and (last_good_date is None or d_str > last_good_date):
                                    last_good_date = d_str
                            except Exception as exc:
                                log.warning("Error processing %s: %s", fname, exc)
                                counters["failed"] += 1
                                iter_failures += 1
                        if _NO_TTY and (_done % _milestone == 0 or _done == len(urls)):
                            print(f"  FHO {year} {mode.upper()}: {_done}/{len(urls)}"
                                  f" ({_done/len(urls)*100:.0f}%)  "
                                  f"got={counters['downloaded']}  "
                                  f"404s={counters['skipped_404']}  "
                                  f"failed={counters['failed']}")

            else:
                # Local directory mode
                local_dir = _FHO_SOURCE
                pattern = os.path.join(local_dir, f"fho_*_{mode}_final.zip")
                zip_files = sorted(glob(pattern))
                if not zip_files:
                    pattern_alt = os.path.join(local_dir, "**", f"fho_*_{mode}_final.zip")
                    zip_files = sorted(glob(pattern_alt, recursive=True))

                year_zips = []
                for zp in zip_files:
                    m = re.search(r"fho_(?:[a-z]+_)?(\d{8})_(am|pm)", os.path.basename(zp))
                    if not m:
                        continue
                    fdate = m.group(1)
                    if not fdate.startswith(str(year)):
                        continue
                    d_obj = datetime.strptime(fdate, "%Y%m%d").date()
                    if start <= d_obj <= end:
                        year_zips.append(zp)

                if not year_zips:
                    log.info("  No local files for FHO %d %s", year, mode.upper())
                    continue

                print(f"\n▶ FHO {year} {mode.upper()}  —  loading {len(year_zips)} local zip files")
                _done = 0
                _milestone = max(1, len(year_zips) // 10)
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = {pool.submit(_load_single_fho_local, zp): zp for zp in year_zips}
                    for fut in tqdm(as_completed(futures), total=len(futures),
                                    desc=f"FHO {year} {mode.upper()} (local)", unit="zip",
                                    disable=_NO_TTY):
                        fname, data = fut.result()
                        _done += 1
                        if data is None:
                            counters["failed"] += 1
                            iter_failures += 1
                        else:
                            counters["downloaded"] += 1
                            try:
                                rows = process_fho_zip_bytes(data, fname, mode)
                                all_rows.extend(rows)
                                d_str = extract_date_from_filename(fname)
                                if d_str and (last_good_date is None or d_str > last_good_date):
                                    last_good_date = d_str
                            except Exception as exc:
                                log.warning("Error processing %s: %s", fname, exc)
                                counters["failed"] += 1
                                iter_failures += 1
                        if _NO_TTY and (_done % _milestone == 0 or _done == len(year_zips)):
                            print(f"  FHO {year} {mode.upper()} (local): {_done}/{len(year_zips)}"
                                  f" ({_done/len(year_zips)*100:.0f}%)  "
                                  f"loaded={counters['downloaded']}  "
                                  f"rows so far={len(all_rows)}  "
                                  f"failed={counters['failed']}")

            if not all_rows:
                log.info("  No data for FHO %d %s", year, mode.upper())
                continue

            print(f"  ✓ FHO {year} {mode.upper()} downloads done  —  "
                  f"{counters['downloaded']} files loaded  |  "
                  f"{counters['skipped_404']} not found (404)  |  "
                  f"{counters['failed']} failed")
            print(f"  Building GeoDataFrame from {len(all_rows):,} raw rows...")
            gdf = gpd.GeoDataFrame(all_rows, geometry="geometry", crs=CRS_TARGET)

            # In update mode, merge with existing layer data before dedup
            layer = f"fho_{year}_{mode}"
            if update_mode and os.path.exists(output_path):
                try:
                    existing = gpd.read_file(output_path, layer=layer)
                    if not existing.empty:
                        log.info("  Merging %d new rows with %d existing rows", len(gdf), len(existing))
                        gdf = pd.concat([existing, gdf], ignore_index=True)
                except Exception as exc:
                    log.warning("  Could not read existing layer %s for merge — "
                                "new data only will be written: %s", layer, exc)

            print(f"  Deduplicating {len(gdf):,} rows...")
            # Dedup: key = geom_wkb_hash + issuance_date + impact_level + forecast_period
            gdf["_geom_hash"] = gdf.geometry.apply(lambda g: hashlib.md5(g.wkb).hexdigest())
            gdf["_dedup"] = gdf["_geom_hash"] + "_" + gdf["issuance_date"] + "_" + gdf["impact_level"] + "_" + gdf["forecast_period"]
            before = len(gdf)
            gdf = gdf.drop_duplicates(subset="_dedup", keep="first").drop(columns=["_dedup", "_geom_hash"])
            log.info("  Dedup: %d → %d (removed %d)", before, len(gdf), before - len(gdf))

            gdf = gdf[gdf["impact_level"] != "Limited_merged"]

            lm = create_limited_inclusive_layer(gdf)
            if not lm.empty:
                gdf = pd.concat([gdf, lm], ignore_index=True)

            area_bin = pd.cut(
                gdf["area_sqkm"],
                bins=[0, 500, 1000, 5000, float("inf")],
                labels=["<500", "500-1000", "1000-5000", ">5000"],
                right=False,
            )
            gdf["area_bin"] = area_bin.cat.add_categories("").fillna("").astype(str)

            _fho_monthly_report(gdf, year, mode, start, end)
            base_for_yc = gdf[gdf["impact_level"] != "Limited_merged"]
            lm_count = len(gdf) - len(base_for_yc)
            print(f"  Writing layer {layer} ({len(base_for_yc):,} polygons"
                  f" + {lm_count:,} Limited_merged)...")
            _write_layer(gdf, output_path, layer)
            counters["processed"] += len(base_for_yc)   # base polygons only; matches YoY table
            print(f"  ✓ Saved {layer}  ({len(base_for_yc):,} base polygons"
                  f" + {lm_count:,} Limited_merged synthetic rows)")

            # Accumulate year_counts (base polygons only) and latest_record
            counters["year_counts"][year] = (
                counters["year_counts"].get(year, 0) + len(base_for_yc)
            )
            if last_good_date:
                lr = datetime.strptime(last_good_date, "%Y%m%d").date()
                if counters["latest_record"] is None or lr > counters["latest_record"]:
                    counters["latest_record"] = lr

            if last_good_date and iter_failures == 0:
                _update_state(state, lambda s: s.setdefault("fho_last_date", {}).__setitem__(key, last_good_date))
            elif last_good_date and iter_failures > 0:
                log.warning("  FHO %s: %d failure(s) — NOT advancing state to avoid gaps",
                            key, iter_failures)

    counters["output_path"] = output_path
    counters["file_size"] = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    return counters


# ===================================================================
# LSR Processing
# ===================================================================

def _chunk_dates(start, end, chunk_days=LSR_CHUNK_DAYS):
    """Yield (chunk_start, chunk_end) pairs."""
    cur = start
    while cur <= end:
        ce = min(cur + timedelta(days=chunk_days - 1), end)
        yield cur, ce
        cur = ce + timedelta(days=1)


def process_lsr(years, output_path, state, update_mode=False):
    """Download + process LSR data → LSRs_flood_allYears.gpkg"""
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "processed": 0,
                "failed_items": [], "year_counts": {}, "latest_record": None}

    overall_start = date(min(years), 1, 1)
    overall_end = min(date(max(years), 12, 31), date.today())

    if update_mode and state.get("lsr_last_date"):
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            log.warning("  State has lsr_last_date but LSR file missing on disk — re-fetching from start")
            state.pop("lsr_last_date", None)
        else:
            resume = datetime.strptime(state["lsr_last_date"], "%Y-%m-%d").date() + timedelta(days=1)
            if resume > overall_end:
                log.info("LSR already up to date")
                counters["output_path"] = output_path
                counters["file_size"] = os.path.getsize(output_path)
                return counters
            print(f"  LSR resuming from: {state['lsr_last_date']}  →  fetching from {resume}")
            overall_start = resume

    all_features = []
    chunks = list(_chunk_dates(overall_start, overall_end))
    print(f"\n▶ LSR  —  fetching {len(chunks)} chunks  ({overall_start} → {overall_end})")

    def _fetch_lsr_chunk(chunk_range):
        """Download a single LSR chunk and return filtered features."""
        cs, ce = chunk_range
        sts = cs.strftime("%Y%m%d1200")
        ets = (ce + timedelta(days=1)).strftime("%Y%m%d1200")
        url = f"{LSR_API}?sts={sts}&ets={ets}"
        resp = http_get(url, timeout=120)
        if resp is None or isinstance(resp, _NotFound):
            return cs, ce, None, []
        try:
            geojson = resp.json()
        except Exception:
            return cs, ce, "bad_json", []
        feats = []
        for feat in geojson.get("features", []):
            props = feat.get("properties", {})
            tt = (props.get("typetext") or "").lower()
            if tt in ("flood", "flash flood"):
                feats.append(feat)
        return cs, ce, "ok", feats

    _lsr_done = 0
    _lsr_milestone = max(1, len(chunks) // 5)
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(chunks))) as pool:
        futures = {pool.submit(_fetch_lsr_chunk, c): c for c in chunks}
        for fut in tqdm(as_completed(futures), total=len(futures),
                        desc="LSR chunks", unit="chunk", disable=_NO_TTY):
            cs, ce, status, feats = fut.result()
            _lsr_done += 1
            if status is None:
                log.warning("LSR chunk %s – %s failed", cs, ce)
                counters["failed"] += 1
                counters["failed_items"].append(f"LSR chunk {cs} – {ce}")
            elif status == "bad_json":
                log.warning("Bad JSON from LSR API for %s–%s", cs, ce)
                counters["failed"] += 1
                counters["failed_items"].append(f"LSR chunk {cs} – {ce} (bad JSON)")
            else:
                counters["downloaded"] += 1
                all_features.extend(feats)
            if _NO_TTY and (_lsr_done % _lsr_milestone == 0 or _lsr_done == len(chunks)):
                print(f"  LSR: {_lsr_done}/{len(chunks)} chunks"
                      f" ({_lsr_done/len(chunks)*100:.0f}%)  "
                      f"features collected={len(all_features):,}  "
                      f"failed={counters['failed']}")

    if not all_features:
        log.warning("No LSR features collected")
        return counters

    print(f"  ✓ LSR fetch done  —  {len(all_features):,} raw features  |  "
          f"{counters['failed']} chunk(s) failed")
    print(f"  Building GeoDataFrame + reprojecting to EPSG:5070...")
    gdf = gpd.GeoDataFrame.from_features(all_features, crs="EPSG:4326")

    # Rename columns per spec
    rename_map = {}
    for src, dst in LSR_FIELD_MAP.items():
        if src in gdf.columns:
            rename_map[src] = dst
    gdf = gdf.rename(columns=rename_map)

    # Ensure VALID is proper datetime (ISO 8601) — EC-11/EC-15
    # Store as tz-naive (implicitly UTC) to match app.py's naive verif windows
    if "VALID" in gdf.columns:
        gdf["VALID"] = pd.to_datetime(gdf["VALID"], errors="coerce", utc=True)
        gdf["VALID"] = gdf["VALID"].dt.tz_convert(None)
        # Drop rows where VALID failed to parse (NaT) — they'd silently vanish in app.py
        nat_count = gdf["VALID"].isna().sum()
        if nat_count > 0:
            log.warning("Dropping %d LSR rows with unparseable VALID timestamps", nat_count)
            gdf = gdf[gdf["VALID"].notna()]

    # Drop malformed rows (EC-17)
    before = len(gdf)
    gdf = gdf[gdf.geometry.notnull()]
    if "LAT" in gdf.columns and "LON" in gdf.columns:
        gdf = gdf[gdf["LAT"].notna() & gdf["LON"].notna()]
        gdf = gdf[(gdf["LAT"].abs() <= 90) & (gdf["LON"].abs() <= 180)]
    after = len(gdf)
    if before != after:
        log.warning("Dropped %d malformed LSR rows", before - after)

    # Reproject
    gdf = gdf.to_crs(CRS_TARGET)

    # In update mode, merge with existing LSR data
    if update_mode and os.path.exists(output_path):
        try:
            existing = gpd.read_file(output_path, layer="LSRs_flood_allYears")
            if not existing.empty:
                if "VALID" in existing.columns:
                    existing["VALID"] = pd.to_datetime(existing["VALID"], errors="coerce", utc=True)
                    existing["VALID"] = existing["VALID"].dt.tz_convert(None)
                log.info("Merging %d new LSRs with %d existing", len(gdf), len(existing))
                gdf = pd.concat([existing, gdf], ignore_index=True)
                gdf = gdf.drop_duplicates(
                    subset=["VALID", "LAT", "LON", "EVENT"], keep="first"
                )
        except Exception as exc:
            log.warning("Could not read existing LSR layer for merge — "
                        "new data only will be written: %s", exc)

    # Year counts and latest record
    if "VALID" in gdf.columns:
        valid_ser = pd.to_datetime(gdf["VALID"], errors="coerce")
        gdf["_yr_tmp"] = valid_ser.dt.year
        counters["year_counts"] = gdf["_yr_tmp"].value_counts().to_dict()
        counters["year_counts"] = {int(k): int(v) for k, v in counters["year_counts"].items()
                                   if pd.notna(k) and int(k) in years}
        lr = valid_ser.max()
        counters["latest_record"] = lr.date() if pd.notna(lr) else None
        gdf = gdf.drop(columns=["_yr_tmp"])

    _lsr_monthly_report(gdf, years)
    print(f"  Writing LSRs_flood_allYears ({len(gdf):,} records)...")
    _write_layer(gdf, output_path, "LSRs_flood_allYears")
    counters["processed"] = len(gdf)
    print(f"  ✓ Saved LSRs_flood_allYears  ({len(gdf):,} records)")

    if counters["failed"] > 0:
        log.warning("LSR: %d chunk(s) failed — NOT advancing lsr_last_date to avoid gaps. "
                     "Re-run without --update or fix failures to fill gaps.", counters["failed"])
    else:
        _update_state(state, lambda s: s.__setitem__("lsr_last_date", overall_end.strftime("%Y-%m-%d")))

    counters["output_path"] = output_path
    counters["file_size"] = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    return counters


# ===================================================================
# WWA Processing
# ===================================================================

def _download_and_process_wwa_year(year):
    """Download and process a single year of WWA data. Returns (year, gdf_or_None, error_str)."""
    url = f"{WWA_PICKUP}/{year}_all.zip"
    tmp = tempfile.mkdtemp(prefix="wwa_")
    zip_path = os.path.join(tmp, f"{year}_all.zip")
    try:
        ok = http_download_to_file(url, zip_path, timeout=300, desc=f"WWA {year}")
        if not ok:
            return year, None, "download_failed"

        try:
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    target = os.path.realpath(os.path.join(tmp, member))
                    if not target.startswith(os.path.realpath(tmp) + os.sep) and target != os.path.realpath(tmp):
                        log.warning("Skipping suspicious zip entry: %s", member)
                        continue
                    zf.extract(member, path=tmp)
        except zipfile.BadZipFile:
            return year, None, "bad_zip"

        shp_files = glob(os.path.join(tmp, "**", "*.shp"), recursive=True)
        if not shp_files:
            return year, None, "no_shapefiles"

        year_gdfs = []
        for shp in shp_files:
            try:
                gdf = gpd.read_file(shp)
            except Exception as exc:
                log.warning("Error reading %s: %s", shp, exc)
                continue

            if "PHENOM" not in gdf.columns:
                continue

            gdf = gdf[gdf["PHENOM"].isin(["FF", "FL"])]

            if "GTYPE" in gdf.columns:
                gdf = gdf[gdf["GTYPE"] == "P"]
            else:
                log.warning("GTYPE column missing in %s — including all geometry types", shp)

            if gdf.empty:
                continue

            gdf = gdf[gdf.geometry.notnull() & gdf.geometry.is_valid]
            gdf["geometry"] = gdf["geometry"].buffer(0)

            if "DAMAGTAG" not in gdf.columns:
                gdf["DAMAGTAG"] = ""
            else:
                gdf["DAMAGTAG"] = gdf["DAMAGTAG"].fillna("")

            gdf = gdf.to_crs(CRS_TARGET)
            gdf["year"] = int(year)
            year_gdfs.append(gdf)

        if year_gdfs:
            combined = pd.concat(year_gdfs, ignore_index=True)
            _wwa_monthly_report(combined, year)
            return year, combined, None
        return year, None, "no_features"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def process_wwa(years, output_path, state, update_mode=False):
    """Download + process WWA data → flood_warnings_all.gpkg"""
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "processed": 0,
                "failed_items": [], "year_counts": {}, "latest_record": None}

    years_to_process = []
    skipped_years = []
    for year in years:
        if update_mode and year in state.get("wwa_years_done", []):
            if not _gpkg_has_layer(output_path, f"wwa_{year}"):
                log.warning("  State says WWA %d is done but layer missing on disk — re-fetching", year)
                state.get("wwa_years_done", []).remove(year)
                years_to_process.append(year)
            else:
                counters["skipped"] += 1
                skipped_years.append(year)
        else:
            years_to_process.append(year)

    if skipped_years:
        print(f"  WWA — years already complete on disk, skipping: {skipped_years}")

    if not years_to_process:
        counters["output_path"] = output_path
        counters["file_size"] = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        return counters

    print(f"\n▶ WWA  —  downloading {len(years_to_process)} year(s): {years_to_process}")
    log.info("WWA – downloading %d year(s) concurrently: %s",
             len(years_to_process), years_to_process)

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(years_to_process))) as pool:
        futures = {pool.submit(_download_and_process_wwa_year, y): y for y in years_to_process}
        for fut in as_completed(futures):
            year, gdf, error = fut.result()
            if error:
                log.warning("WWA %d: %s", year, error)
                counters["failed"] += 1
                counters["failed_items"].append(f"WWA year {year} ({error})")
                continue
            counters["downloaded"] += 1
            layer = f"wwa_{year}"
            print(f"  WWA {year} — writing {len(gdf):,} warnings...")
            _write_layer(gdf, output_path, layer)
            counters["processed"] += len(gdf)
            counters["year_counts"][year] = len(gdf)
            print(f"  ✓ Saved {layer}  ({len(gdf):,} FF/FL warnings)")
            # Track latest ISSUED date across years
            if "ISSUED" in gdf.columns:
                lr = pd.to_datetime(gdf["ISSUED"], errors="coerce").max()
                if pd.notna(lr):
                    lr_d = lr.date() if hasattr(lr, "date") else lr
                    if counters["latest_record"] is None or lr_d > counters["latest_record"]:
                        counters["latest_record"] = lr_d
            _yr = year  # bind for closure safety
            def _mark_done(s, y=_yr):
                done = s.setdefault("wwa_years_done", [])
                if y not in done:
                    done.append(y)
            _update_state(state, _mark_done)

    counters["output_path"] = output_path
    counters["file_size"] = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    return counters


# ===================================================================
# Main
# ===================================================================

def _print_config_banner(args, datasets, update_mode, mode_source):
    """Print a startup configuration banner so the user can confirm the run parameters."""
    src_label = {
        "nwc": f"NWC  ({FHO_HOST})",
    }.get(args.fho_source, f"Local dir  ({args.fho_source})")
    mode_label = f"{'Incremental' if update_mode else 'Full run'}  [{mode_source}]"
    years_str = "  ".join(str(y) for y in sorted(args.years))
    ds_str    = "  ".join(d.upper() for d in datasets)

    W = 62
    bar = "═" * W
    print(f"\n╔{bar}╗")
    print(f"║{'FHO EVALUATION PIPELINE':^{W}}║")
    print(f"╠{bar}╣")
    print(f"║  {'Started':<14}: {datetime.now().strftime('%Y-%m-%d  %H:%M:%S'):<{W-18}}║")
    print(f"║  {'Datasets':<14}: {ds_str:<{W-18}}║")
    print(f"║  {'Years':<14}: {years_str:<{W-18}}║")
    print(f"║  {'FHO source':<14}: {src_label:<{W-18}}║")
    print(f"║  {'Mode':<14}: {mode_label:<{W-18}}║")
    print(f"║  {'Workers':<14}: {args.workers:<{W-18}}║")
    print(f"╚{bar}╝\n")


def _print_run_summary(all_counters, total_elapsed, parallel):
    """Print the final pipeline run summary table with staleness check."""
    # Column widths
    C = {"ds": 9, "ela": 10, "dl": 11, "s404": 9, "skip": 8, "fail": 7, "rec": 18}

    def row(ds, ela, dl, s404, skip, fail, rec, size):
        size_str = f"/ {_format_size(size)}" if size else ""
        rec_str  = f"{rec:,} {size_str}".strip()
        return (f"║ {ds:<{C['ds']}} ║ {ela:>{C['ela']}} ║ {dl:>{C['dl']},} ║"
                f" {s404:>{C['s404']},} ║ {skip:>{C['skip']},} ║ {fail:>{C['fail']},} ║"
                f" {rec_str:<{C['rec']}} ║")

    def _div(l, m, r):
        return (l + "═"*(C['ds']+2) + m + "═"*(C['ela']+2) + m + "═"*(C['dl']+2) + m +
                "═"*(C['s404']+2) + m + "═"*(C['skip']+2) + m + "═"*(C['fail']+2) + m +
                "═"*(C['rec']+2) + r)

    top = _div("╔","╤","╗"); div = _div("╠","╦","╣")
    mid = _div("╠","╪","╣"); bot = _div("╚","╧","╝")
    hdr = (f"║ {'Dataset':<{C['ds']}} ║ {'Elapsed':>{C['ela']}} ║ {'Downloaded':>{C['dl']}} ║"
           f" {'FHO 404s':>{C['s404']}} ║ {'Skipped':>{C['skip']}} ║ {'Failed':>{C['fail']}} ║"
           f" {'Records / Size':<{C['rec']}} ║")

    note = "  (datasets ran concurrently — elapsed times overlap)" if parallel else ""
    title = "PIPELINE RUN SUMMARY" + note
    inner_w = sum(C.values()) + len(C) * 3 - 1
    print(f"\n{top}")
    print(f"║{title:^{inner_w}}║")
    print(div)
    print(hdr)
    print(div)

    t_dl = t_s404 = t_skip = t_fail = t_rec = t_size = 0
    for ds in ("fho", "lsr", "wwa"):
        if ds not in all_counters:
            continue
        c    = all_counters[ds]
        ela  = _format_elapsed(c.get("elapsed", 0))
        dl   = c.get("downloaded", 0)
        s404 = c.get("skipped_404", 0)
        skip = c.get("skipped", 0)
        fail = c.get("failed", 0)
        rec  = c.get("processed", 0)
        size = c.get("file_size", 0)
        t_dl += dl; t_s404 += s404; t_skip += skip; t_fail += fail; t_rec += rec; t_size += size
        print(row(ds.upper(), ela, dl, s404, skip, fail, rec, size))

    print(mid)
    print(row("TOTAL", _format_elapsed(total_elapsed), t_dl, t_s404, t_skip, t_fail, t_rec, t_size))
    print(bot)
    print(f"  * FHO 404s   = days with no issuance on the NWC server (normal — FHO is issued every")
    print(f"                 day but server 404s occur when files aren't yet posted or for archive gaps)")
    print(f"  * FHO Records = base polygons only (Limited/Considerable/Catastrophic/Unknown).")
    print(f"                 Synthetic Limited_merged rows are written to the GeoPackage but excluded here.")

    # --- Data freshness ---
    today = date.today()
    stale_thresholds = {"fho": 3, "lsr": 7, "wwa": 60}
    # FHO staleness is only meaningful when pulling live from NWC;
    # Local FHO archives are bounded by whatever was saved — not actionable.
    fho_is_offline = _FHO_SOURCE != "nwc"
    # WWA staleness is only meaningful when the current year was included in the run;
    # for purely historical runs the latest record will always be end-of-last-year.
    wwa_year_counts = all_counters.get("wwa", {}).get("year_counts", {})
    wwa_includes_current_year = date.today().year in wwa_year_counts
    any_stale = False
    freshness_lines = []
    for ds in ("fho", "lsr", "wwa"):
        if ds not in all_counters:
            continue
        lr = all_counters[ds].get("latest_record")
        if not lr:
            freshness_lines.append(f"    {ds.upper():<6}  latest record: unknown")
            continue
        if isinstance(lr, str):
            try:
                lr = datetime.strptime(lr, "%Y%m%d").date()
            except ValueError:
                try:
                    lr = datetime.fromisoformat(lr).date()
                except Exception:
                    freshness_lines.append(f"    {ds.upper():<6}  latest record: {lr}")
                    continue
        elif hasattr(lr, "date"):
            lr = lr.date()
        days_old = (today - lr).days
        thresh = stale_thresholds.get(ds, 7)
        offline = (ds == "fho" and fho_is_offline) or (ds == "wwa" and not wwa_includes_current_year)
        if offline:
            label = "local archive" if ds == "fho" else "historical run — current year not included"
            marker = f"  ({label} — staleness not checked)"
        elif days_old > thresh:
            marker = f"  !! STALE ({days_old}d ago — threshold {thresh}d)"
            any_stale = True
        else:
            marker = f"  ok  ({days_old}d ago)"
        freshness_lines.append(f"    {ds.upper():<6}  latest record: {lr}  {marker}")

    if freshness_lines:
        print(f"\n  DATA FRESHNESS")
        print(f"  {'─'*54}")
        for line in freshness_lines:
            print(line)
        if any_stale:
            print(f"\n  !! One or more datasets appear stale. Re-run or check the source.")
    print()


def _print_yoy_table(all_counters, years):
    """Print a year-over-year comparison of record counts across all three datasets."""
    fho_yc  = all_counters.get("fho", {}).get("year_counts", {})
    lsr_yc  = all_counters.get("lsr", {}).get("year_counts", {})
    wwa_yc  = all_counters.get("wwa", {}).get("year_counts", {})

    if not any([fho_yc, lsr_yc, wwa_yc]):
        return

    all_years = sorted(set(years) | set(fho_yc) | set(lsr_yc) | set(wwa_yc))

    # Find peak year per dataset
    fho_peak = max(fho_yc, key=fho_yc.get) if fho_yc else None
    lsr_peak = max(lsr_yc, key=lsr_yc.get) if lsr_yc else None
    wwa_peak = max(wwa_yc, key=wwa_yc.get) if wwa_yc else None

    sep = "  " + "─" * 58
    print(f"\n{sep}")
    print(f"  YEAR-OVER-YEAR COMPARISON")
    print(sep)
    print(f"  {'Year':<7} {'FHO Polygons':>13} {'LSR Events':>12} {'WWA Warnings':>14}")
    print(f"  {'─'*6}  {'─'*13}  {'─'*12}  {'─'*14}")
    fho_tot = lsr_tot = wwa_tot = 0
    for yr in all_years:
        fho_n = fho_yc.get(yr, 0); lsr_n = lsr_yc.get(yr, 0); wwa_n = wwa_yc.get(yr, 0)
        fho_tot += fho_n; lsr_tot += lsr_n; wwa_tot += wwa_n
        fho_mark = " ◀" if yr == fho_peak else "  "
        lsr_mark = " ◀" if yr == lsr_peak else "  "
        wwa_mark = " ◀" if yr == wwa_peak else "  "
        print(f"  {yr:<7} {fho_n:>11,}{fho_mark} {lsr_n:>10,}{lsr_mark} {wwa_n:>12,}{wwa_mark}")
    print(f"  {'─'*6}  {'─'*13}  {'─'*12}  {'─'*14}")
    print(f"  {'TOTAL':<7} {fho_tot:>13,} {lsr_tot:>12,} {wwa_tot:>14,}")
    print(f"{sep}\n")


def _print_failure_details(all_counters):
    """If any dataset had failures, list the specific items that failed."""
    any_failures = any(
        all_counters.get(ds, {}).get("failed_items")
        for ds in ("fho", "lsr", "wwa")
    )
    if not any_failures:
        return

    sep = "  " + "─" * 54
    print(f"\n{sep}")
    print(f"  FAILED ITEMS  (re-run to retry these)")
    print(sep)
    for ds in ("fho", "lsr", "wwa"):
        items = all_counters.get(ds, {}).get("failed_items", [])
        if not items:
            continue
        print(f"\n  {ds.upper()} ({len(items)} failure{'s' if len(items)>1 else ''}):")
        for item in items[:50]:   # cap at 50 so it doesn't flood the terminal
            print(f"    • {item}")
        if len(items) > 50:
            print(f"    ... and {len(items)-50} more (see log for full list)")
    print(f"{sep}\n")


def _ensure_utf8_stdio():
    """Avoid UnicodeEncodeError on Windows (cp1252) when printing box-drawing / arrows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="FHO Evaluation Data Pipeline")
    parser.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS,
                        help=f"Years to process (default: 2022–{date.today().year})")
    parser.add_argument("--only", choices=["fho", "lsr", "wwa"],
                        help="Process only one dataset")
    parser.add_argument("--full", action="store_true",
                        help="Force a complete re-run, ignoring pipeline_state.json")
    parser.add_argument("--update", action="store_true", help=argparse.SUPPRESS)  # backwards compat
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help="Concurrent download workers (default: 6)")
    parser.add_argument("--fho-source", default="nwc",
                        help="FHO data source: 'nwc' (NWC server) or a local directory path "
                             "containing FHO zip files (fho_YYYYMMDD_am|pm_final.zip)")
    args = parser.parse_args()
    _ensure_utf8_stdio()

    if str(args.fho_source).lower() == "gdrive":
        log.error(
            "Google Drive as --fho-source is no longer supported (folder web views do not list "
            "every file). Sync FHO zips to disk (e.g. Google Drive for desktop) and pass that "
            "folder path, or use the default NWC source."
        )
        sys.exit(1)

    # Apply module-level config overrides
    _set_max_workers(args.workers)
    global _FHO_SOURCE
    _FHO_SOURCE = args.fho_source

    datasets = [args.only] if args.only else ["fho", "lsr", "wwa"]

    # --- Resolve run mode ---
    # Load state first so auto-detect can inspect it.
    state = load_state()
    if args.full:
        update_mode = False
        mode_source = "--full flag"
    elif args.update:
        update_mode = True
        mode_source = "--update flag"
    elif _state_has_data(state):
        update_mode = True
        mode_source = "auto-detected from pipeline_state.json"
    else:
        update_mode = False
        mode_source = "no prior state — first run"

    _print_config_banner(args, datasets, update_mode, mode_source)

    t0 = time.time()
    all_counters = {}
    parallel = len(datasets) > 1

    if parallel:
        log.info("Running %d datasets concurrently: %s", len(datasets),
                 [d.upper() for d in datasets])

        def _run_dataset(ds_name):
            t_ds = time.time()
            if ds_name == "fho":
                c = process_fho(args.years, "fho_all.gpkg", state, update_mode=update_mode)
            elif ds_name == "lsr":
                c = process_lsr(args.years, "LSRs_flood_allYears.gpkg", state, update_mode=update_mode)
            elif ds_name == "wwa":
                c = process_wwa(args.years, "flood_warnings_all.gpkg", state, update_mode=update_mode)
            else:
                c = {}
            c["elapsed"] = time.time() - t_ds
            return ds_name, c

        with ThreadPoolExecutor(max_workers=len(datasets)) as pool:
            futures = [pool.submit(_run_dataset, ds) for ds in datasets]
            for fut in as_completed(futures):
                ds_name, counters = fut.result()
                all_counters[ds_name] = counters
                log.info("Finished %s  (%s)", ds_name.upper(),
                         _format_elapsed(counters.get("elapsed", 0)))
        # _update_state() already persists to disk on each call — no extra save needed
    else:
        ds = datasets[0]
        log.info("=" * 60)
        log.info("Processing %s data", ds.upper())
        log.info("=" * 60)
        t_ds = time.time()
        if ds == "fho":
            c = process_fho(args.years, "fho_all.gpkg", state, update_mode=update_mode)
        elif ds == "lsr":
            c = process_lsr(args.years, "LSRs_flood_allYears.gpkg", state, update_mode=update_mode)
        elif ds == "wwa":
            c = process_wwa(args.years, "flood_warnings_all.gpkg", state, update_mode=update_mode)
        else:
            c = {}
        c["elapsed"] = time.time() - t_ds
        all_counters[ds] = c
        save_state(state)

    total_elapsed = time.time() - t0
    _print_run_summary(all_counters, total_elapsed, parallel)
    _print_yoy_table(all_counters, args.years)
    _print_failure_details(all_counters)


if __name__ == "__main__":
    main()
