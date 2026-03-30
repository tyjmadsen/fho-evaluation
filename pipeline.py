#!/usr/bin/env python3
"""
Unified FHO Evaluation Pipeline
Downloads and processes FHO, LSR, and WWA data into GeoPackage files.

Usage:
    python pipeline.py                          # Full run, all years 2022-2025
    python pipeline.py --years 2024 2025        # Specific years only
    python pipeline.py --only fho               # Only process FHO dataset
    python pipeline.py --update                 # Incremental update from last state
    python pipeline.py --fho-source gdrive      # Download FHO from Google Drive
    python pipeline.py --fho-source /path/to/zips  # Use local FHO zip directory
    python pipeline.py --browser chrome         # Use Chrome cookies (default: edge)
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

# Suppress SSL warnings (NWC server uses self-signed certs)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_YEARS = list(range(2022, 2026))
CRS_TARGET = "EPSG:5070"

FHO_HOST = "https://ops.nwc.nws.noaa.gov"
LSR_API = "https://mesonet.agron.iastate.edu/geojson/lsr.php"
WWA_PICKUP = "https://mesonet.agron.iastate.edu/pickup/wwa"

# Google Drive folder containing FHO shapefiles (fho/shpzip/*.zip)
GDRIVE_FOLDER_ID = "1wEyZUUs0L090CIxCN9wk45kzBo33pX0I"

STATE_FILE = "pipeline_state.json"
MAX_WORKERS = 6
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, doubled each retry
LSR_CHUNK_DAYS = 90

# FHO source mode — set by CLI args
_FHO_SOURCE = "nwc"        # "nwc", "gdrive", or a local directory path
_BROWSER_FOR_COOKIES = "edge"  # "edge" or "chrome"


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
# Google Drive helpers
# ---------------------------------------------------------------------------

def _get_gdrive_session():
    """Create a requests session with browser cookies for Google Drive auth."""
    try:
        import browser_cookie3
    except ImportError:
        log.error("browser-cookie3 is required for Google Drive downloads. "
                  "Install with: pip install browser-cookie3")
        sys.exit(1)

    cookie_loaders = {
        "edge": browser_cookie3.edge,
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
    }
    loader = cookie_loaders.get(_BROWSER_FOR_COOKIES)
    if not loader:
        log.error("Unsupported browser: %s (use edge, chrome, or firefox)", _BROWSER_FOR_COOKIES)
        sys.exit(1)

    try:
        cj = loader(domain_name=".google.com")
    except Exception as exc:
        log.error("Failed to load cookies from %s: %s", _BROWSER_FOR_COOKIES, exc)
        log.error("Make sure you're logged into Google Drive in %s", _BROWSER_FOR_COOKIES)
        sys.exit(1)

    sess = requests.Session()
    sess.cookies = cj
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    return sess


def _gdrive_list_folder(sess, folder_id):
    """List all files in a Google Drive folder using the web interface.

    Uses the internal Google Drive web endpoint that works with browser cookies,
    since the REST API requires OAuth tokens.
    Returns list of (file_id, filename, mimeType).
    """
    files = []
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    resp = sess.get(url, timeout=30)
    if resp.status_code != 200:
        log.error("Could not access Google Drive folder (HTTP %d). "
                  "Make sure you're logged in.", resp.status_code)
        return []

    # Drive embeds file data as JSON in the page source — extract IDs and names
    # Pattern: file entries appear as arrays with [file_id, filename, ...] in the JS
    text = resp.text

    # Method 1: Parse from the embedded data in the page
    # Google Drive embeds file metadata in a specific JS structure
    id_name_pairs = re.findall(
        r'\["([\w_-]{20,})",\s*"(fho_\d{8}_(?:am|pm)_final\.zip)"',
        text
    )
    for fid, fname in id_name_pairs:
        files.append((fid, fname, "application/zip"))

    # Also look for folder entries
    folder_pairs = re.findall(
        r'\["([\w_-]{20,})",\s*"([^"]+?)",\s*"application/vnd\.google-apps\.folder"',
        text
    )
    for fid, fname in folder_pairs:
        files.append((fid, fname, "application/vnd.google-apps.folder"))

    if not files:
        # Method 2: Try gdown's folder listing as fallback
        try:
            import gdown
            file_list = gdown.download_folder(
                id=folder_id, skip_download=True,
                quiet=True, remaining_ok=True
            )
            if file_list:
                for f in file_list:
                    files.append((f.id, f.name, ""))
        except Exception as exc:
            log.debug("gdown folder listing fallback failed: %s", exc)

    return files


def _gdrive_navigate_to_shpzip(sess, root_folder_id):
    """Navigate folder → fho/fop → shpzip and return the shpzip folder ID."""
    items = _gdrive_list_folder(sess, root_folder_id)
    fho_folder = None
    for fid, fname, mime in items:
        if fname.lower() in ("fho", "fop") and "folder" in mime:
            fho_folder = fid
            break

    if not fho_folder:
        log.warning("Could not find 'fho' subfolder in Drive root — "
                    "trying root as shpzip folder directly")
        return root_folder_id

    items = _gdrive_list_folder(sess, fho_folder)
    for fid, fname, mime in items:
        if fname.lower() == "shpzip" and "folder" in mime:
            return fid

    log.warning("Could not find 'shpzip' subfolder — using 'fho' folder directly")
    return fho_folder


def _gdrive_download_file(sess, file_id, filename):
    """Download a single file from Google Drive. Returns bytes or None."""
    url = f"https://drive.google.com/uc?id={file_id}&export=download&confirm=t"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = sess.get(url, stream=True, timeout=120)
            if resp.status_code != 200:
                log.debug("GDrive download %s returned %d", filename, resp.status_code)
                return None
            content = resp.content
            if b"html" in content[:200].lower():
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(content, "html.parser")
                    form = soup.find("form", {"id": "download-form"})
                    if form and "action" in form.attrs:
                        inputs = form.find_all("input", {"type": "hidden"})
                        params = {inp["name"]: inp["value"] for inp in inputs}
                        redirect_url = form["action"]
                        resp2 = sess.get(redirect_url, params=params, stream=True, timeout=120)
                        if resp2.status_code == 200:
                            content = resp2.content
                    else:
                        log.warning("GDrive returned HTML (not a download form) for %s", filename)
                        return None
                except ImportError:
                    log.warning("GDrive returned HTML for %s but bs4 not installed", filename)
                    return None
            if not content[:4].startswith(b'PK'):
                log.warning("GDrive download for %s is not a valid zip file (got %d bytes)", filename, len(content))
                return None
            return content
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                log.warning("GDrive download failed after %d attempts: %s — %s",
                            MAX_RETRIES, filename, exc)
                return None
            time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
    return None


def _get_gdrive_fho_zips(years, modes):
    """Get list of (filename, file_id) for FHO zips matching the requested years/modes."""
    sess = _get_gdrive_session()
    shpzip_id = _gdrive_navigate_to_shpzip(sess, GDRIVE_FOLDER_ID)
    all_files = _gdrive_list_folder(sess, shpzip_id)

    if not all_files:
        log.error("No files found in Google Drive shpzip folder")
        return [], sess

    log.info("Found %d files in Google Drive shpzip folder", len(all_files))

    matching = []
    for fid, fname, _ in all_files:
        if not fname.endswith(".zip"):
            continue
        m = re.search(r"fho_(\d{4})\d{4}_(am|pm)", fname)
        if not m:
            continue
        year = int(m.group(1))
        mode = m.group(2)
        if year in years and mode in modes:
            matching.append((fname, fid))

    return matching, sess


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


# ===================================================================
# FHO Processing
# ===================================================================

def extract_date_from_filename(path):
    m = re.search(r"fho_(\d{8})_(am|pm)", path)
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


_gdrive_thread_local = threading.local()


def _download_single_fho_gdrive(args):
    """Download a single FHO zip from Google Drive. Returns (filename, bytes) or (filename, None)."""
    fname, file_id, template_sess = args
    if not hasattr(_gdrive_thread_local, 'sess'):
        s = requests.Session()
        s.cookies = template_sess.cookies.copy()
        s.headers.update(template_sess.headers)
        _gdrive_thread_local.sess = s
    data = _gdrive_download_file(_gdrive_thread_local.sess, file_id, fname)
    return fname, data


def _load_single_fho_local(zip_path):
    """Load a single FHO zip from local disk. Returns (filename, bytes) or (filename, None)."""
    fname = os.path.basename(zip_path)
    try:
        with open(zip_path, "rb") as f:
            return fname, f.read()
    except Exception as exc:
        log.warning("Error reading local file %s: %s", zip_path, exc)
        return fname, None


def process_fho(years, output_path, state, update_mode=False):
    """Download + process all FHO data → fho_all.gpkg"""
    counters = {"downloaded": 0, "skipped_404": 0, "failed": 0, "processed": 0}

    # Pre-fetch Google Drive file listing if needed (once for all years)
    gdrive_sess = None
    gdrive_file_map = {}  # fname -> file_id
    if _FHO_SOURCE == "gdrive":
        log.info("Connecting to Google Drive (using %s cookies)...", _BROWSER_FOR_COOKIES)
        gdrive_files, gdrive_sess = _get_gdrive_fho_zips(years, ("am", "pm"))
        if not gdrive_files:
            log.error("No matching FHO files found on Google Drive")
            return counters
        for fname, fid in gdrive_files:
            gdrive_file_map[fname] = fid
        log.info("Found %d matching FHO zip files on Google Drive", len(gdrive_file_map))

    for year in years:
        for mode in ("am", "pm"):
            log.info("FHO %d %s – preparing downloads", year, mode.upper())
            key = f"{year}_{mode}"

            # Determine date range
            start = date(year, 1, 1)
            end = min(date(year, 12, 31), date.today())
            if update_mode and key in state.get("fho_last_date", {}):
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

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = {pool.submit(_download_single_fho, u): u for u in urls}
                    for fut in tqdm(as_completed(futures), total=len(futures),
                                    desc=f"FHO {year} {mode.upper()}", unit="zip"):
                        fname, data, status = fut.result()
                        if status == "404":
                            counters["skipped_404"] += 1
                            continue
                        if status == "failed":
                            counters["failed"] += 1
                            iter_failures += 1
                            continue
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

            elif _FHO_SOURCE == "gdrive":
                # Filter GDrive files for this year+mode
                year_mode_files = []
                for fname, fid in gdrive_file_map.items():
                    m = re.search(r"fho_(\d{8})_(am|pm)", fname)
                    if not m:
                        continue
                    fdate = m.group(1)
                    fmode = m.group(2)
                    if fmode != mode or not fdate.startswith(str(year)):
                        continue
                    d_obj = datetime.strptime(fdate, "%Y%m%d").date()
                    if start <= d_obj <= end:
                        year_mode_files.append((fname, fid))

                if not year_mode_files:
                    log.info("  No GDrive files for FHO %d %s", year, mode.upper())
                    continue

                work_items = [(fn, fid, gdrive_sess) for fn, fid in year_mode_files]
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = {pool.submit(_download_single_fho_gdrive, w): w[0] for w in work_items}
                    for fut in tqdm(as_completed(futures), total=len(futures),
                                    desc=f"FHO {year} {mode.upper()} (GDrive)", unit="zip"):
                        fname, data = fut.result()
                        if data is None:
                            counters["skipped_404"] += 1
                            continue
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
                    m = re.search(r"fho_(\d{8})_(am|pm)", os.path.basename(zp))
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

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = {pool.submit(_load_single_fho_local, zp): zp for zp in year_zips}
                    for fut in tqdm(as_completed(futures), total=len(futures),
                                    desc=f"FHO {year} {mode.upper()} (local)", unit="zip"):
                        fname, data = fut.result()
                        if data is None:
                            counters["failed"] += 1
                            iter_failures += 1
                            continue
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

            if not all_rows:
                log.info("  No data for FHO %d %s", year, mode.upper())
                continue

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

            _write_layer(gdf, output_path, layer)
            counters["processed"] += len(gdf)
            log.info("  Saved layer %s (%d polygons)", layer, len(gdf))

            if last_good_date and iter_failures == 0:
                _update_state(state, lambda s: s.setdefault("fho_last_date", {}).__setitem__(key, last_good_date))
            elif last_good_date and iter_failures > 0:
                log.warning("  FHO %s: %d failure(s) — NOT advancing state to avoid gaps",
                            key, iter_failures)

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
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "processed": 0}

    overall_start = date(min(years), 1, 1)
    overall_end = min(date(max(years), 12, 31), date.today())

    if update_mode and state.get("lsr_last_date"):
        resume = datetime.strptime(state["lsr_last_date"], "%Y-%m-%d").date() + timedelta(days=1)
        if resume > overall_end:
            log.info("LSR already up to date")
            return counters
        overall_start = resume

    all_features = []
    chunks = list(_chunk_dates(overall_start, overall_end))

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

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(chunks))) as pool:
        futures = {pool.submit(_fetch_lsr_chunk, c): c for c in chunks}
        for fut in tqdm(as_completed(futures), total=len(futures),
                        desc="LSR chunks", unit="chunk"):
            cs, ce, status, feats = fut.result()
            if status is None:
                log.warning("LSR chunk %s – %s failed", cs, ce)
                counters["failed"] += 1
            elif status == "bad_json":
                log.warning("Bad JSON from LSR API for %s–%s", cs, ce)
                counters["failed"] += 1
            else:
                counters["downloaded"] += 1
                all_features.extend(feats)

    if not all_features:
        log.warning("No LSR features collected")
        return counters

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

    _write_layer(gdf, output_path, "LSRs_flood_allYears")
    counters["processed"] = len(gdf)
    log.info("Saved %d LSRs to %s", len(gdf), output_path)

    if counters["failed"] > 0:
        log.warning("LSR: %d chunk(s) failed — NOT advancing lsr_last_date to avoid gaps. "
                     "Re-run without --update or fix failures to fill gaps.", counters["failed"])
    else:
        _update_state(state, lambda s: s.__setitem__("lsr_last_date", overall_end.strftime("%Y-%m-%d")))

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
            return year, pd.concat(year_gdfs, ignore_index=True), None
        return year, None, "no_features"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def process_wwa(years, output_path, state, update_mode=False):
    """Download + process WWA data → flood_warnings_all.gpkg"""
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "processed": 0}

    years_to_process = []
    for year in years:
        if update_mode and year in state.get("wwa_years_done", []):
            log.info("WWA %d already done, skipping", year)
            counters["skipped"] += 1
        else:
            years_to_process.append(year)

    if not years_to_process:
        return counters

    log.info("WWA – downloading %d year(s) concurrently: %s",
             len(years_to_process), years_to_process)

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(years_to_process))) as pool:
        futures = {pool.submit(_download_and_process_wwa_year, y): y for y in years_to_process}
        for fut in as_completed(futures):
            year, gdf, error = fut.result()
            if error:
                log.warning("WWA %d: %s", year, error)
                counters["failed"] += 1
                continue
            counters["downloaded"] += 1
            layer = f"wwa_{year}"
            _write_layer(gdf, output_path, layer)
            counters["processed"] += len(gdf)
            log.info("Created %s records", f"{len(gdf):,}")
            log.info("  Saved layer %s (%d features)", layer, len(gdf))
            _yr = year  # bind for closure safety
            def _mark_done(s, y=_yr):
                done = s.setdefault("wwa_years_done", [])
                if y not in done:
                    done.append(y)
            _update_state(state, _mark_done)

    return counters


# ===================================================================
# Main
# ===================================================================

def main():
    parser = argparse.ArgumentParser(description="FHO Evaluation Data Pipeline")
    parser.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS,
                        help="Years to process (default: 2022-2025)")
    parser.add_argument("--only", choices=["fho", "lsr", "wwa"],
                        help="Process only one dataset")
    parser.add_argument("--update", action="store_true",
                        help="Incremental update from pipeline_state.json")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help="Concurrent download workers (default: 6)")
    parser.add_argument("--fho-source", default="nwc",
                        help="FHO data source: 'nwc' (NWC server), 'gdrive' (Google Drive), "
                             "or a local directory path containing FHO zip files")
    parser.add_argument("--browser", default="edge", choices=["edge", "chrome", "firefox"],
                        help="Browser to extract cookies from for Google Drive auth (default: edge)")
    args = parser.parse_args()

    # Apply module-level config overrides
    _set_max_workers(args.workers)
    global _FHO_SOURCE, _BROWSER_FOR_COOKIES
    _FHO_SOURCE = args.fho_source
    _BROWSER_FOR_COOKIES = args.browser

    state = load_state()
    t0 = time.time()
    all_counters = {}

    datasets = [args.only] if args.only else ["fho", "lsr", "wwa"]

    if len(datasets) > 1:
        # Run independent datasets concurrently — each writes to its own file
        log.info("Running %d datasets concurrently: %s", len(datasets),
                 [d.upper() for d in datasets])

        def _run_dataset(ds_name):
            if ds_name == "fho":
                return ds_name, process_fho(
                    args.years, "fho_all.gpkg", state, update_mode=args.update)
            elif ds_name == "lsr":
                return ds_name, process_lsr(
                    args.years, "LSRs_flood_allYears.gpkg", state, update_mode=args.update)
            elif ds_name == "wwa":
                return ds_name, process_wwa(
                    args.years, "flood_warnings_all.gpkg", state, update_mode=args.update)

        with ThreadPoolExecutor(max_workers=len(datasets)) as pool:
            futures = [pool.submit(_run_dataset, ds) for ds in datasets]
            for fut in as_completed(futures):
                ds_name, counters = fut.result()
                all_counters[ds_name] = counters
                log.info("Finished %s processing", ds_name.upper())
        # _update_state() already persists to disk on each call — no extra save needed
    else:
        # Single dataset mode — run sequentially
        ds = datasets[0]
        log.info("=" * 60)
        log.info("Processing %s data", ds.upper())
        log.info("=" * 60)
        if ds == "fho":
            all_counters["fho"] = process_fho(
                args.years, "fho_all.gpkg", state, update_mode=args.update)
        elif ds == "lsr":
            all_counters["lsr"] = process_lsr(
                args.years, "LSRs_flood_allYears.gpkg", state, update_mode=args.update)
        elif ds == "wwa":
            all_counters["wwa"] = process_wwa(
                args.years, "flood_warnings_all.gpkg", state, update_mode=args.update)
        save_state(state)

    elapsed = time.time() - t0
    log.info("=" * 60)
    log.info("PIPELINE COMPLETE  (%.1f seconds)", elapsed)
    log.info("=" * 60)
    for ds, c in all_counters.items():
        log.info(
            "  %s: downloaded=%d  skipped=%d  failed=%d  processed=%d",
            ds.upper(),
            c.get("downloaded", 0),
            c.get("skipped", 0) + c.get("skipped_404", 0),
            c.get("failed", 0),
            c.get("processed", 0),
        )


if __name__ == "__main__":
    main()
