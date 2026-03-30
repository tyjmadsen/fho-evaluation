from flask import Flask, render_template, jsonify, request, Response
import geopandas as gpd
import pandas as pd
import numpy as np
from datetime import date as dt_date, datetime, time as dt_time, timedelta
from shapely.ops import unary_union
from tqdm import tqdm
import os
import io
import csv
import hashlib
import json
import warnings
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape as html_escape
import threading
import traceback

warnings.filterwarnings('ignore', message='Boolean Series key will be reindexed', category=UserWarning)

app = Flask(__name__)


class CustomJSONProvider(app.json_provider_class):
    def default(self, obj):
        if obj is pd.NaT or (isinstance(obj, type(pd.NaT)) and pd.isna(obj)):
            return None
        if isinstance(obj, np.datetime64) and np.isnat(obj):
            return None
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            if np.isnan(obj) or np.isinf(obj):
                return None
            return float(obj)
        if isinstance(obj, pd.Timestamp):
            if pd.isna(obj):
                return None
            return obj.isoformat()
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dt_date):
            return obj.isoformat()
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


app.json_provider_class = CustomJSONProvider
app.json = CustomJSONProvider(app)

# ── Server-side response cache ────────────────────────────────────────────────
class ResponseCache:
    """Thread-safe LRU cache for API responses keyed by normalized request parameters."""
    def __init__(self, maxsize=128):
        self._cache = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def _make_key(self, params):
        normalized = json.dumps(params, sort_keys=True)
        return hashlib.md5(normalized.encode()).hexdigest()

    def get(self, params):
        key = self._make_key(params)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def put(self, params, response):
        key = self._make_key(params)
        with self._lock:
            self._cache[key] = response
            self._cache.move_to_end(key)
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def clear(self):
        with self._lock:
            self._cache.clear()

_stats_cache = ResponseCache(maxsize=32)
_ibw_cache = ResponseCache(maxsize=32)

# ── Geometry helpers ──────────────────────────────────────────────────────────
DISPLAY_SIMPLIFY_TOLERANCE = 0.0005  # ~50m in EPSG:4326, invisible at typical zoom

def _simplify_for_display(geom):
    """Simplify geometry for map display (reduces vertex count without visual loss)."""
    if geom is None or geom.is_empty:
        return geom
    return geom.simplify(DISPLAY_SIMPLIFY_TOLERANCE, preserve_topology=True)

def _round_coords(geojson_dict, precision=5):
    """Round coordinate precision in a GeoJSON geometry dict to reduce payload size."""
    if geojson_dict is None:
        return None

    def _round_seq(seq):
        if isinstance(seq, (list, tuple)):
            if seq and isinstance(seq[0], (int, float)):
                return [round(c, precision) for c in seq]
            return [_round_seq(item) for item in seq]
        return seq

    if 'coordinates' in geojson_dict:
        geojson_dict = dict(geojson_dict)
        geojson_dict['coordinates'] = _round_seq(geojson_dict['coordinates'])
    elif 'geometries' in geojson_dict:
        geojson_dict = dict(geojson_dict)
        geojson_dict['geometries'] = [_round_coords(g, precision) for g in geojson_dict['geometries']]
    return geojson_dict

def _safe_geo_interface(geom, simplify=True):
    """Safely convert a geometry to a rounded GeoJSON dict, returning None if empty/null."""
    if geom is None or geom.is_empty:
        return None
    if simplify:
        geom = _simplify_for_display(geom)
    if geom is None or geom.is_empty:
        return None
    return _round_coords(geom.__geo_interface__)

def _safe_escape(val):
    """HTML-escape a value for popup content."""
    if val is None:
        return 'Unknown'
    try:
        if pd.isna(val):
            return 'Unknown'
    except (ValueError, TypeError):
        pass
    return html_escape(str(val))

# Cache for loaded data
DATA_CACHE = {}
_GPKG_FILES = ['fho_all.gpkg', 'LSRs_flood_allYears.gpkg', 'flood_warnings_all.gpkg']
_high_impact_events = None  # Pre-computed at load time
_data_lock = threading.Lock()
_last_failed_reload = 0.0  # epoch timestamp of last failed reload attempt
_RELOAD_BACKOFF_SECS = 30  # minimum seconds between retry after a failed reload


def _gpkg_mtimes():
    """Return a tuple of modification times for the three GeoPackage files."""
    result = []
    for f in _GPKG_FILES:
        try:
            result.append(os.path.getmtime(f))
        except OSError:
            result.append(None)
    return tuple(result)


def gdf_to_feature_collection(gdf, popup_builder=None, simplify=True):
    """Convert a GeoDataFrame to a GeoJSON FeatureCollection without iterrows().

    Uses vectorized .to_dict('records') and bulk geometry export for speed.
    Optional popup_builder(props_dict) can inject popup_content into properties.
    Coordinates are rounded and geometries simplified for smaller payloads.
    """
    if gdf is None or gdf.empty:
        return {'type': 'FeatureCollection', 'features': []}

    geoms = gdf.geometry.values
    records = gdf.drop(columns='geometry').to_dict('records')

    def _clean_value(v):
        """Convert numpy/pandas types to JSON-safe Python types."""
        if v is None:
            return None
        if isinstance(v, np.bool_):
            return bool(v)
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, (float, np.floating)):
            return None if (np.isnan(v) or np.isinf(v)) else float(v)
        if isinstance(v, pd.Timestamp):
            return None if pd.isna(v) else v.isoformat()
        if isinstance(v, dt_date):
            return v.isoformat()
        if isinstance(v, (str, list, dict, bool, int)):
            return v
        try:
            if pd.isna(v):
                return None
        except (ValueError, TypeError):
            pass
        return v

    features = []
    for rec, geom in zip(records, geoms):
        if geom is None or geom.is_empty:
            continue
        props = {k: _clean_value(v) for k, v in rec.items()}
        if popup_builder:
            popup_builder(props)
        display_geom = _simplify_for_display(geom) if simplify else geom
        if display_geom is None or display_geom.is_empty:
            continue
        features.append({
            'type': 'Feature',
            'geometry': _round_coords(display_geom.__geo_interface__),
            'properties': props
        })
    return {'type': 'FeatureCollection', 'features': features}


def _lsr_popup(props):
    """Inject popup_content for LSR features."""
    event_type = _safe_escape(props.get('EVENT') or props.get('TYPETEXT') or 'Unknown')
    remarks = _safe_escape(props.get('REMARKS') or props.get('REMARK') or 'None')
    props['popup_content'] = (
        f"<b>LSR Details:</b><br>"
        f"Event: {event_type}<br>"
        f"Location: {_safe_escape(props.get('CITY', 'Unknown'))}, {_safe_escape(props.get('STATE', 'Unknown'))}<br>"
        f"Time: {_safe_escape(props.get('VALID', 'Unknown'))}<br>"
        f"Source: {_safe_escape(props.get('SOURCE', 'Unknown'))}<br>"
        f"Remarks: {remarks}"
    )


def _ffw_popup(props):
    """Inject popup_content for FFW features."""
    tag = props.get('DAMAGTAG') or 'No Tag'
    props['popup_content'] = (
        f"<b>Flood Warning Details:</b><br>"
        f"Issued: {_safe_escape(props.get('ISSUED', 'Unknown'))}<br>"
        f"Expired: {_safe_escape(props.get('EXPIRED', 'Unknown'))}<br>"
        f"Phenomena: {_safe_escape(props.get('PHENOM', 'Unknown'))}<br>"
        f"Impact: {_safe_escape(tag)}"
    )


def load_layer(args):
    """Helper function to load a single layer."""
    year, period = args
    layer_name = f'fho_{year}_{period}'
    try:
        layer = gpd.read_file('fho_all.gpkg', layer=layer_name).to_crs("EPSG:4326")
        print(f"Successfully loaded {layer_name}")
        return layer
    except Exception as e:
        print(f"Could not read layer {layer_name}: {e}")
        return None

def load_warning_layer(year):
    """Helper function to load a single warning layer."""
    try:
        ffw = gpd.read_file("flood_warnings_all.gpkg", layer=f"wwa_{year}").to_crs("EPSG:4326")
        print(f"Successfully loaded flood warnings for {year}")
        return ffw
    except Exception as e:
        print(f"Could not read flood warnings for {year}: {e}")
        return None

# Load data with caching
def load_data():
    current_mtimes = _gpkg_mtimes()
    if ('fho_areas' in DATA_CACHE and 'lsrs' in DATA_CACHE and 'ffws' in DATA_CACHE
            and DATA_CACHE.get('_mtimes') == current_mtimes):
        return DATA_CACHE['fho_areas'], DATA_CACHE['lsrs'], DATA_CACHE['ffws']

    print("Loading FHO data...")
    years = range(2022, dt_date.today().year + 1)  # 2022 through current year
    periods = ['am', 'pm']
    
    # Parallel loading of FHO layers
    with ThreadPoolExecutor(max_workers=4) as executor:
        fho_layers = []
        futures = [executor.submit(load_layer, (year, period)) 
                  for year in years for period in periods]
        
        for future in tqdm(as_completed(futures), total=len(futures), desc="Loading FHO layers"):
            layer = future.result()
            if layer is not None:
                fho_layers.append(layer)

    print("Combining FHO data...")
    fho_areas = pd.concat(fho_layers, ignore_index=True) if fho_layers else None
    if fho_areas is None or fho_areas.empty:
        print("Warning: No FHO areas were loaded successfully")
        return None, None, None
    print(f"Loaded {len(fho_areas)} FHO areas")

    print("Loading LSR data...")
    try:
        try:
            lsrs = gpd.read_file("LSRs_flood_allYears.gpkg", layer="LSRs_flood_allYears").to_crs("EPSG:4326")
        except (ValueError, KeyError):
            lsrs = gpd.read_file("LSRs_flood_allYears.gpkg").to_crs("EPSG:4326")
        print(f"Loaded {len(lsrs)} LSRs")
    except Exception as e:
        print(f"Warning: Could not read LSR data: {e}")
        lsrs = gpd.GeoDataFrame(columns=['VALID', 'EVENT', 'CITY', 'STATE', 'SOURCE',
                                          'REMARKS', 'geometry'], geometry='geometry',
                                 crs="EPSG:4326")
        print("Continuing with empty LSR dataset")

    print("Loading flood warnings...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        ffws_list = []
        futures = [executor.submit(load_warning_layer, year) for year in years]
        
        for future in tqdm(as_completed(futures), total=len(futures), desc="Loading flood warnings"):
            layer = future.result()
            if layer is not None:
                ffws_list.append(layer)

    print("Combining flood warnings...")
    if ffws_list:
        ffws = pd.concat(ffws_list, ignore_index=True)
        print(f"Loaded {len(ffws)} flood warnings")
    else:
        print("Warning: No flood warnings were loaded successfully — continuing with empty FFW dataset")
        ffws = gpd.GeoDataFrame(columns=['PHENOM', 'ISSUED', 'EXPIRED', 'DAMAGTAG',
                                          'year', 'geometry'], geometry='geometry',
                                 crs="EPSG:4326")

    print("Processing timestamps...")
    lsrs["VALID"] = pd.to_datetime(lsrs["VALID"], errors="coerce", utc=True).dt.tz_convert(None)
    ffws["ISSUED"] = pd.to_datetime(ffws["ISSUED"], errors="coerce", utc=True).dt.tz_convert(None)
    ffws["EXPIRED"] = pd.to_datetime(ffws["EXPIRED"], errors="coerce", utc=True).dt.tz_convert(None)
    ffws = ffws.dropna(subset=["ISSUED", "EXPIRED"])

    ffws = ffws[ffws["PHENOM"].isin(["FF", "FL"])]
    if "DAMAGTAG" not in ffws.columns:
        ffws["DAMAGTAG"] = ""
    else:
        ffws["DAMAGTAG"] = ffws["DAMAGTAG"].fillna("")

    # ENH-2: Pre-parse dates at load time
    fho_areas['valid_start'] = pd.to_datetime(fho_areas['valid_start'], errors="coerce", utc=True).dt.tz_convert(None)
    nat_fho = fho_areas['valid_start'].isna().sum()
    if nat_fho > 0:
        print(f"Warning: Dropping {nat_fho} FHO rows with unparseable valid_start")
        fho_areas = fho_areas[fho_areas['valid_start'].notna()]
    fho_areas['valid_date'] = fho_areas['valid_start'].dt.date

    # ENH-3: Build spatial index at load time
    _ = lsrs.sindex
    _ = ffws.sindex

    # Pre-compute high-impact events (eliminates slow apply() on every request)
    hi_events = _build_high_impact_events(fho_areas, ffws)

    # Atomically swap all cache entries so concurrent requests never see partial state
    global _high_impact_events
    new_cache = {
        'fho_areas': fho_areas,
        'lsrs': lsrs,
        'ffws': ffws,
        '_mtimes': current_mtimes,
        '_high_impact_events': hi_events,
        '_datasets': (fho_areas, lsrs, ffws),
    }
    DATA_CACHE.update(new_cache)
    _high_impact_events = hi_events

    # Clear response caches when data reloads
    _stats_cache.clear()
    _ibw_cache.clear()

    print("Data loading complete!")
    return fho_areas, lsrs, ffws


def _build_high_impact_events(fho_df, ffws_df):
    """Pre-compute high-impact event lists (fully vectorized)."""
    result = {'considerable_fho': [], 'catastrophic_fho': [], 'high_impact_ffws': []}
    if fho_df is None or ffws_df is None or fho_df.empty:
        return result

    for level, key in [('Considerable', 'considerable_fho'), ('Catastrophic', 'catastrophic_fho')]:
        subset = fho_df[fho_df['impact_level'] == level]
        if subset.empty:
            continue
        if 'issuance_date' in subset.columns:
            # issuance_date stored as "YYYYMMDD" — normalise to "YYYY-MM-DD" for FFW comparison
            raw = subset['issuance_date'].astype(str)
            dates = pd.to_datetime(raw, format='%Y%m%d', errors='coerce').dt.strftime('%Y-%m-%d')
        else:
            dates = subset['valid_start'].dt.strftime('%Y-%m-%d')
        unique = pd.DataFrame({
            'date': dates.values,
            'issuance': subset['issuance_time'].values,
            'period': subset['forecast_period'].values,
        }).drop_duplicates()
        result[key] = unique.to_dict('records')

    # Use issuance_date (not valid_start) to determine which dates have FHO coverage
    fho_date_set = set(d['date'] for d in result['considerable_fho'] + result['catastrophic_fho'])
    hi_ffws = ffws_df[ffws_df['DAMAGTAG'].isin(['CONSIDERABLE', 'CATASTROPHIC'])]
    if not hi_ffws.empty:
        ffw_dates = hi_ffws['ISSUED'].dt.strftime('%Y-%m-%d')
        no_fho_mask = ~ffw_dates.isin(fho_date_set)
        no_fho = hi_ffws[no_fho_mask]
        if not no_fho.empty:
            ffw_df = pd.DataFrame({
                'date': no_fho['ISSUED'].dt.strftime('%Y-%m-%d').values,
                'tag': no_fho['DAMAGTAG'].values,
                'issued': no_fho['ISSUED'].dt.strftime('%H:%M:%S').values,
                'expired': no_fho['EXPIRED'].dt.strftime('%H:%M:%S').values,
            }).drop_duplicates(subset=['date', 'tag'])
            result['high_impact_ffws'] = ffw_df.to_dict('records')

    return result

# Load data at startup (load_data() populates DATA_CACHE['_datasets'] atomically)
load_data()


def _get_datasets():
    """Atomically read the current dataset tuple."""
    return DATA_CACHE.get('_datasets', (None, None, None))


def _check_reload():
    """Reload data if GeoPackage files have changed since last load."""
    global _high_impact_events, _last_failed_reload
    current = _gpkg_mtimes()
    if current != DATA_CACHE.get('_mtimes'):
        import time as _time
        if _time.time() - _last_failed_reload < _RELOAD_BACKOFF_SECS:
            return
        with _data_lock:
            if current != DATA_CACHE.get('_mtimes'):
                print("GeoPackage files changed, reloading...")
                new_fho, new_lsrs, new_ffws = load_data()
                if new_fho is not None:
                    _high_impact_events = DATA_CACHE.get('_high_impact_events')
                else:
                    _last_failed_reload = _time.time()
                    print("Warning: Reload failed — keeping previous data, "
                          f"will retry in {_RELOAD_BACKOFF_SECS}s")

_AM_TIME = dt_time(12, 0, 0)   # 12:00 UTC (7 AM CDT / 6 AM CST)
_PM_TIME = dt_time(21, 0, 0)   # 21:00 UTC (4 PM CDT / 3 PM CST)

_PERIOD_OFFSETS = {
    "1-3": (0, 3),
    "4-7": (3, 7),
    "1-7": (0, 7),
}


def get_date_range(issuance_time, forecast_period, fho_issuance_date):
    """Get the date range for a given forecast period based on FHO issuance date.
    
    All times are in UTC to match the FHO data and verification data.
    """
    offsets = _PERIOD_OFFSETS.get(forecast_period)
    if offsets is None:
        return None, None

    start_days, end_days = offsets
    t = _AM_TIME if str(issuance_time).strip().lower() == "am" else _PM_TIME

    start_date = datetime.combine(fho_issuance_date + timedelta(days=start_days), t)
    end_date = datetime.combine(fho_issuance_date + timedelta(days=end_days), t)

    return start_date, end_date

@app.before_request
def _maybe_reload():
    if request.path.startswith('/api/'):
        _check_reload()


@app.route('/')
def index():
    return render_template('fho_evaluation.html')

@app.route('/api/available-dates', methods=['GET'])
def get_available_dates():
    """Get a list of dates where FHO data is available."""
    try:
        _fho = _get_datasets()[0]
        if _fho is None:
            return jsonify({'error': 'Data not loaded. Check GeoPackage files.'}), 500
        
        dates = sorted(d for d in _fho['valid_date'].unique()
                       if d is not None and not (isinstance(d, float) and np.isnan(d)))
        date_strings = [d.strftime('%Y-%m-%d') for d in dates]
        
        return jsonify(date_strings)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Failed to load available dates'}), 500

def normalize_issuance(raw):
    """Normalize issuance value to 'am' or 'pm' regardless of input format (AM/PM/am/pm)."""
    val = str(raw).strip().lower()
    if val in ('am', '12z'):
        return 'am'
    if val in ('pm', '21z'):
        return 'pm'
    return val


def issuance_time_mask(series: pd.Series, normalized: str) -> pd.Series:
    """Match issuance_time to normalized 'am'/'pm' (handles NaN and non-strings)."""
    return series.fillna('').astype(str).str.strip().str.lower() == normalized


@app.route('/api/stats', methods=['POST'])
def get_stats():
    filters = request.get_json(silent=True)
    if not filters:
        return jsonify({'error': 'Request body is required'}), 400

    missing = [f for f in ('issuance_date', 'issuance', 'forecast_period') if not filters.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    # Snapshot data references atomically so a mid-request reload can't swap them
    _fho_areas, _lsrs, _ffws = _get_datasets()

    if _fho_areas is None or _lsrs is None or _ffws is None:
        return jsonify({'error': 'Data not loaded. Check GeoPackage files.'}), 500

    # Check server-side cache
    cache_params = {k: filters.get(k) for k in ('issuance_date', 'end_date', 'issuance', 'forecast_period')}
    cached = _stats_cache.get(cache_params)
    if cached is not None:
        return jsonify(cached)

    try:
        try:
            start_date = pd.to_datetime(filters['issuance_date']).date()
        except (ValueError, TypeError):
            return jsonify({'error': f'Invalid issuance_date: {filters["issuance_date"]}'}), 400
        end_date_raw = (filters.get('end_date') or '').strip()
        if end_date_raw:
            try:
                end_date = pd.to_datetime(end_date_raw).date()
            except (ValueError, TypeError):
                return jsonify({'error': f'Invalid end_date: {end_date_raw}'}), 400
        else:
            end_date = start_date
        forecast_period = filters['forecast_period']
        if forecast_period not in _PERIOD_OFFSETS:
            return jsonify({'error': f'Invalid forecast_period: {forecast_period}. Must be one of {list(_PERIOD_OFFSETS.keys())}'}), 400
        if end_date < start_date:
            return jsonify({
                'error': 'Invalid date range: End date cannot be before the FHO Issuance Date'
            }), 400
        if (end_date - start_date).days > 366:
            return jsonify({
                'error': 'Date range too large. Maximum range is 366 days.'
            }), 400

        issuance_time = normalize_issuance(filters['issuance'])
        # Client sends pod_threshold for URL/state parity; verification is binary (>=1 event per polygon).
        min_events = 1

        # Define filters that will be used for both statistics and map data
        issuance_filter = issuance_time_mask(_fho_areas['issuance_time'], issuance_time)
        impact_filter = (_fho_areas['impact_level'] == 'Limited_merged')
        period_filter = (_fho_areas['forecast_period'] == forecast_period)

        # Get all FHO areas in the date range
        date_range_filter = (
            (_fho_areas['valid_date'] >= start_date) &
            (_fho_areas['valid_date'] <= end_date)
        )
        range_fho = _fho_areas[date_range_filter & period_filter & issuance_filter & impact_filter]

        total_polygons = len(range_fho)
        above_threshold = 0
        below_threshold = 0
        verified_polygon_indices = set()

        if not range_fho.empty:
            for vdate, group in range_fho.groupby('valid_date'):
                verif_start, verif_end = get_date_range(issuance_time, forecast_period, vdate)
                if verif_start is None or verif_end is None:
                    below_threshold += len(group)
                    continue

                period_lsrs = _lsrs[
                    (_lsrs['VALID'] >= verif_start) &
                    (_lsrs['VALID'] < verif_end)
                ]
                period_ffws = _ffws[
                    (_ffws['ISSUED'] <= verif_end) &
                    (_ffws['EXPIRED'] >= verif_start)
                ]

                if period_lsrs.empty and period_ffws.empty:
                    below_threshold += len(group)
                    continue

                lsr_hits = gpd.sjoin(period_lsrs, group, predicate='intersects', how='inner') if not period_lsrs.empty else pd.DataFrame()
                ffw_hits = gpd.sjoin(period_ffws, group, predicate='intersects', how='inner') if not period_ffws.empty else pd.DataFrame()

                lsr_counts = lsr_hits.groupby('index_right').size() if not lsr_hits.empty else pd.Series(dtype=int)
                ffw_counts = ffw_hits.groupby('index_right').size() if not ffw_hits.empty else pd.Series(dtype=int)
                hits_per_poly = lsr_counts.add(ffw_counts, fill_value=0).reindex(group.index, fill_value=0)

                for idx in group.index:
                    if hits_per_poly.get(idx, 0) >= min_events:
                        above_threshold += 1
                        verified_polygon_indices.add(idx)
                    else:
                        below_threshold += 1

        # Get map data for the selected FHO issuance date
        map_data = get_empty_geometries()
        selected_date_filter = (_fho_areas['valid_date'] == start_date)
        selected_fho = _fho_areas[selected_date_filter & period_filter & issuance_filter & impact_filter]
        
        if not selected_fho.empty:
            selected_verif_start, selected_verif_end = get_date_range(issuance_time, forecast_period, start_date)
            
            if selected_verif_start is not None and selected_verif_end is not None:
                selected_merged = unary_union(selected_fho.geometry)
                merged_gdf = gpd.GeoDataFrame(geometry=[selected_merged], crs=_fho_areas.crs)
                selected_merged = merged_gdf.geometry.iloc[0]
                
                selected_lsrs = _lsrs[
                    (_lsrs['VALID'] >= selected_verif_start) &
                    (_lsrs['VALID'] < selected_verif_end)
                ]
                selected_ffws = _ffws[
                    (_ffws['ISSUED'] <= selected_verif_end) &
                    (_ffws['EXPIRED'] >= selected_verif_start)
                ]
                
                map_lsrs_hit = selected_lsrs[selected_lsrs.intersects(selected_merged)]
                map_ffws_hit = selected_ffws[selected_ffws.intersects(selected_merged)]
                map_lsrs_miss = selected_lsrs[~selected_lsrs.index.isin(map_lsrs_hit.index)]
                map_ffws_miss = selected_ffws[~selected_ffws.index.isin(map_ffws_hit.index)]
                
                # Per-polygon verification via spatial join (vectorized)
                sel_wgs = selected_fho.copy()

                lsr_counts_per_poly = pd.Series(0, index=sel_wgs.index, dtype=int)
                ffw_counts_per_poly = pd.Series(0, index=sel_wgs.index, dtype=int)

                if not selected_lsrs.empty:
                    lsr_join = gpd.sjoin(selected_lsrs, sel_wgs, predicate='intersects', how='inner')
                    if not lsr_join.empty:
                        lsr_counts_per_poly = lsr_join.groupby('index_right').size().reindex(sel_wgs.index, fill_value=0)

                if not selected_ffws.empty:
                    ffw_join = gpd.sjoin(selected_ffws, sel_wgs, predicate='intersects', how='inner')
                    if not ffw_join.empty:
                        ffw_counts_per_poly = ffw_join.groupby('index_right').size().reindex(sel_wgs.index, fill_value=0)

                # Vectorized per-polygon feature building
                sel_wgs['lsr_hits'] = lsr_counts_per_poly.astype(int)
                sel_wgs['ffw_hits'] = ffw_counts_per_poly.astype(int)
                sel_wgs['hit_count'] = sel_wgs['lsr_hits'] + sel_wgs['ffw_hits']
                sel_wgs['verified'] = sel_wgs['hit_count'] >= 1
                sel_wgs['forecast_period'] = forecast_period
                sel_wgs['issuance_time'] = issuance_time.upper()
                sel_wgs['issuance_date'] = start_date.strftime('%Y-%m-%d')

                # Build features using to_dict('records') + bulk geometry
                keep_cols = ['polygon_id', 'area_sqkm', 'area_bin', 'impact_level',
                             'forecast_period', 'issuance_time', 'issuance_date',
                             'hit_count', 'verified', 'lsr_hits', 'ffw_hits',
                             'centroid_x', 'centroid_y']
                cols = [c for c in keep_cols if c in sel_wgs.columns]
                records = sel_wgs[cols].to_dict('records')
                geoms = sel_wgs.geometry.values

                def _safe_round(val, ndigits):
                    """Round a numeric value, returning 0 for NaN/None."""
                    try:
                        fv = float(val)
                        return 0 if np.isnan(fv) or np.isinf(fv) else round(fv, ndigits)
                    except (TypeError, ValueError):
                        return 0

                poly_features = []
                for rec, geom in zip(records, geoms):
                    if geom is None or geom.is_empty:
                        continue
                    if 'area_sqkm' in rec:
                        rec['area_sqkm'] = _safe_round(rec.get('area_sqkm', 0), 1)
                    if 'centroid_x' in rec:
                        rec['centroid_x'] = _safe_round(rec.get('centroid_x', 0), 4)
                    if 'centroid_y' in rec:
                        rec['centroid_y'] = _safe_round(rec.get('centroid_y', 0), 4)
                    rec['hit_count'] = int(rec.get('hit_count', 0))
                    rec['lsr_hits'] = int(rec.get('lsr_hits', 0))
                    rec['ffw_hits'] = int(rec.get('ffw_hits', 0))
                    rec['verified'] = bool(rec.get('verified', False))
                    simplified = _simplify_for_display(geom)
                    if simplified is None or simplified.is_empty:
                        continue
                    poly_features.append({
                        'type': 'Feature',
                        'geometry': _round_coords(simplified.__geo_interface__),
                        'properties': rec
                    })

                map_data = {
                    'fho': {
                        'type': 'Feature',
                        'geometry': _safe_geo_interface(selected_merged),
                        'properties': {}
                    },
                    'fho_polygons': {
                        'type': 'FeatureCollection',
                        'features': poly_features
                    },
                    'lsrs_hit': gdf_to_feature_collection(map_lsrs_hit, _lsr_popup),
                    'lsrs_miss': gdf_to_feature_collection(map_lsrs_miss, _lsr_popup),
                    'ffws_hit': gdf_to_feature_collection(map_ffws_hit, _ffw_popup),
                    'ffws_miss': gdf_to_feature_collection(map_ffws_miss, _ffw_popup),
                }
            else:
                map_data = get_empty_geometries()
        else:
            map_data = get_empty_geometries()

        # ── Event-based hit / miss counts ───────────────────────────────
        # For EACH issuance date in the user's range, compute a single
        # verification window, merge that date's FHO polygons, then
        # classify events as hit/miss.  Cumulative sets prevent
        # double-counting events that appear in overlapping windows.
        # Per-day stats only count NEW events not seen in prior days.
        daily_stats = []
        seen_lsr_hits = set()
        seen_lsr_misses = set()
        seen_ffw_hits = set()
        seen_ffw_misses = set()

        user_gave_range = bool(filters.get('end_date'))

        if user_gave_range:
            loop_start = start_date
            loop_end   = end_date
        else:
            loop_start = start_date
            loop_end   = start_date

        all_seen = set()

        check_date = loop_start
        while check_date <= loop_end:
            date_filter = (_fho_areas['valid_date'] == check_date)
            fho_for_date = _fho_areas[date_filter & issuance_filter & impact_filter]

            if not fho_for_date.empty:
                fho_filtered = fho_for_date[fho_for_date['forecast_period'] == forecast_period]

                if not fho_filtered.empty:
                    verif_start, verif_end = get_date_range(issuance_time, forecast_period, check_date)

                    if verif_start is not None and verif_end is not None:
                        merged_polygon = unary_union(fho_filtered.geometry)

                        lsrs_valid = _lsrs[
                            (_lsrs['VALID'] >= verif_start) &
                            (_lsrs['VALID'] < verif_end)
                        ]
                        ffws_valid = _ffws[
                            (_ffws['ISSUED'] <= verif_end) &
                            (_ffws['EXPIRED'] >= verif_start)
                        ]

                        lsrs_hit = lsrs_valid[lsrs_valid.intersects(merged_polygon)]
                        ffws_hit = ffws_valid[ffws_valid.intersects(merged_polygon)]
                        lsrs_miss = lsrs_valid[~lsrs_valid.index.isin(lsrs_hit.index)]
                        ffws_miss = ffws_valid[~ffws_valid.index.isin(ffws_hit.index)]

                        seen_lsr_hits.update(lsrs_hit.index.tolist())
                        seen_ffw_hits.update(ffws_hit.index.tolist())
                        seen_lsr_misses.update(lsrs_miss.index.tolist())
                        seen_ffw_misses.update(ffws_miss.index.tolist())

                        # Per-day polygon verification via sjoin
                        day_polys = len(fho_filtered)
                        day_verified = 0
                        if not lsrs_valid.empty or not ffws_valid.empty:
                            lsr_j = gpd.sjoin(lsrs_valid, fho_filtered, predicate='intersects', how='inner') if not lsrs_valid.empty else pd.DataFrame()
                            ffw_j = gpd.sjoin(ffws_valid, fho_filtered, predicate='intersects', how='inner') if not ffws_valid.empty else pd.DataFrame()
                            lsr_c = lsr_j.groupby('index_right').size() if not lsr_j.empty else pd.Series(dtype=int)
                            ffw_c = ffw_j.groupby('index_right').size() if not ffw_j.empty else pd.Series(dtype=int)
                            combined = lsr_c.add(ffw_c, fill_value=0).reindex(fho_filtered.index, fill_value=0)
                            day_verified = int((combined >= min_events).sum())

                        new_lsr_h = set(lsrs_hit.index) - all_seen
                        new_lsr_m = set(lsrs_miss.index) - all_seen
                        new_ffw_h = set(ffws_hit.index) - all_seen
                        new_ffw_m = set(ffws_miss.index) - all_seen
                        all_seen.update(lsrs_hit.index, lsrs_miss.index,
                                        ffws_hit.index, ffws_miss.index)

                        daily_stats.append({
                            'date': check_date,
                            'lsr_hits': len(new_lsr_h),
                            'lsr_misses': len(new_lsr_m),
                            'ffw_hits': len(new_ffw_h),
                            'ffw_misses': len(new_ffw_m),
                            'polygons_verified': day_verified,
                            'polygons_total': day_polys
                        })
                    else:
                        daily_stats.append({'date': check_date, 'lsr_hits': 0, 'lsr_misses': 0, 'ffw_hits': 0, 'ffw_misses': 0, 'polygons_verified': 0, 'polygons_total': len(fho_filtered)})
                else:
                    daily_stats.append({'date': check_date, 'lsr_hits': 0, 'lsr_misses': 0, 'ffw_hits': 0, 'ffw_misses': 0, 'polygons_verified': 0, 'polygons_total': 0})
            else:
                daily_stats.append({'date': check_date, 'lsr_hits': 0, 'lsr_misses': 0, 'ffw_hits': 0, 'ffw_misses': 0, 'polygons_verified': 0, 'polygons_total': 0})

            check_date += timedelta(days=1)

        # Events that were hit in any window are hits; remove from misses
        seen_lsr_misses -= seen_lsr_hits
        seen_ffw_misses -= seen_ffw_hits

        total_lsr_hits = len(seen_lsr_hits)
        total_lsr_misses = len(seen_lsr_misses)
        total_ffw_hits = len(seen_ffw_hits)
        total_ffw_misses = len(seen_ffw_misses)
        total_hits = total_lsr_hits + total_ffw_hits
        total_misses = total_lsr_misses + total_ffw_misses

        # ── Event capture rate ─────────────────────────────────────────────
        total_events = total_hits + total_misses
        event_capture_rate = total_hits / total_events if total_events > 0 else 0

        # ── Area stats (informational) ───────────────────────────────────
        total_fho_area = float(range_fho['area_sqkm'].sum()) if 'area_sqkm' in range_fho.columns and not range_fho.empty else 0
        if total_fho_area > 0 and 'area_sqkm' in range_fho.columns:
            verified_area = float(range_fho.loc[range_fho.index.isin(verified_polygon_indices), 'area_sqkm'].sum())
        else:
            verified_area = 0

        # Area-bin stratified verification rate
        area_bin_stats = []
        if not range_fho.empty and 'area_bin' in range_fho.columns:
            range_fho_copy = range_fho.copy()
            range_fho_copy['_verified'] = range_fho_copy.index.isin(verified_polygon_indices)
            for bin_label, bin_group in range_fho_copy.groupby('area_bin'):
                bin_total = len(bin_group)
                bin_verified = int(bin_group['_verified'].sum())
                area_bin_stats.append({
                    'bin': str(bin_label),
                    'total': bin_total,
                    'verified': bin_verified,
                    'pod': round(bin_verified / bin_total * 100, 1) if bin_total > 0 else 0
                })
            bin_order = {'<500': 0, '500-1000': 1, '1000-5000': 2, '>5000': 3}
            area_bin_stats.sort(key=lambda x: bin_order.get(x['bin'], 99))

        # Daily time series — polygon verification rate per day
        daily_series = []
        for ds in daily_stats:
            daily_series.append({
                'date': ds['date'].strftime('%Y-%m-%d'),
                'polygons_verified': ds.get('polygons_verified', 0),
                'polygons_total': ds.get('polygons_total', 0),
                'lsr_inside': ds['lsr_hits'],
                'lsr_outside': ds['lsr_misses'],
                'ffw_inside': ds['ffw_hits'],
                'ffw_outside': ds['ffw_misses'],
                'verification_rate': round(ds['polygons_verified'] / ds['polygons_total'] * 100, 1) if ds.get('polygons_total', 0) > 0 else 0
            })

        sel_vs, sel_ve_start = get_date_range(issuance_time, forecast_period, start_date)
        _, sel_ve = get_date_range(issuance_time, forecast_period, end_date)
        if sel_vs is not None and sel_ve is not None:
            verif_window = {'start': sel_vs.isoformat(), 'end': sel_ve.isoformat()}
        elif sel_vs is not None and sel_ve_start is not None:
            verif_window = {'start': sel_vs.isoformat(), 'end': sel_ve_start.isoformat()}
        else:
            verif_window = {}

        days_included_set = {stats['date'].strftime('%Y-%m-%d') for stats in daily_stats}

        response = {
            'statistics': {
                'event_capture_rate': round(event_capture_rate, 4),
                'total_events': total_events,
                'verified_polygons': above_threshold,
                'unverified_polygons': below_threshold,
                'total_polygons': total_polygons,
                'total_days': len(daily_stats),
                'total_fho_area_sqkm': round(total_fho_area, 1),
                'verified_area_sqkm': round(verified_area, 1),
                'days_included': [stats['date'].strftime('%Y-%m-%d') for stats in daily_stats],
                'days_excluded': [d.strftime('%Y-%m-%d') for d in pd.date_range(start_date, end_date)
                                if d.strftime('%Y-%m-%d') not in days_included_set]
            },
            'evidence': {
                'lsr_inside': total_lsr_hits,
                'lsr_outside': total_lsr_misses,
                'ffw_inside': total_ffw_hits,
                'ffw_outside': total_ffw_misses,
                'total_inside': total_hits,
                'total_outside': total_misses,
            },
            'daily_series': daily_series,
            'area_bin_stats': area_bin_stats,
            'geometries': map_data,
            'verification_window': verif_window
        }

        _stats_cache.put(cache_params, response)
        return jsonify(response)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error processing stats request'}), 500

def get_empty_geometries():
    """Return empty geometry collections for FHO verification page."""
    return {
        'fho': {'type': 'Feature', 'geometry': None, 'properties': {}},
        'fho_polygons': {'type': 'FeatureCollection', 'features': []},
        'lsrs_hit': {'type': 'FeatureCollection', 'features': []},
        'lsrs_miss': {'type': 'FeatureCollection', 'features': []},
        'ffws_hit': {'type': 'FeatureCollection', 'features': []},
        'ffws_miss': {'type': 'FeatureCollection', 'features': []}
    }


def get_empty_ibw_geometries():
    """Return empty geometry collections for IBW validation page."""
    return {
        'limited': {'type': 'FeatureCollection', 'features': []},
        'fho_considerable': None,
        'fho_catastrophic': None,
        'hits': {'type': 'FeatureCollection', 'features': []},
        'misses': {'type': 'FeatureCollection', 'features': []},
        'other_impact': {'type': 'FeatureCollection', 'features': []},
        'no_tag': {'type': 'FeatureCollection', 'features': []},
        'lsrs': {'type': 'FeatureCollection', 'features': []}
    }

@app.route('/ibw-validation')
def ibw_validation():
    return render_template('ibw_validation.html')

@app.route('/api/ibw-stats', methods=['POST'])
def get_ibw_stats():
    filters = request.get_json(silent=True)
    if not filters:
        return jsonify({'error': 'Request body is required'}), 400

    missing = [f for f in ('issuance_date', 'issuance', 'forecast_period') if not filters.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    # Snapshot data references atomically
    _fho_areas, _lsrs, _ffws = _get_datasets()

    if _fho_areas is None or _lsrs is None or _ffws is None:
        return jsonify({'error': 'Data not loaded. Check GeoPackage files.'}), 500

    # Check server-side cache
    ibw_cache_params = {k: filters.get(k) for k in ('issuance_date', 'issuance', 'forecast_period', 'impact_level')}
    cached = _ibw_cache.get(ibw_cache_params)
    if cached is not None:
        return jsonify(cached)

    try:
        try:
            start_date = pd.to_datetime(filters['issuance_date']).date()
        except (ValueError, TypeError):
            return jsonify({'error': f'Invalid issuance_date: {filters["issuance_date"]}'}), 400
        forecast_period = filters['forecast_period']
        if forecast_period not in _PERIOD_OFFSETS:
            return jsonify({'error': f'Invalid forecast_period: {forecast_period}. Must be one of {list(_PERIOD_OFFSETS.keys())}'}), 400
        impact_level = filters.get('impact_level', 'Considerable')
        if impact_level not in ('Considerable', 'Catastrophic'):
            return jsonify({'error': f'Invalid impact_level: {impact_level}. Must be Considerable or Catastrophic'}), 400

        issuance_time = normalize_issuance(filters['issuance'])
        
        # Filter FHO polygons
        fho_considerable = _fho_areas[
            (_fho_areas['valid_date'] == start_date) &
            issuance_time_mask(_fho_areas['issuance_time'], issuance_time) &
            (_fho_areas['impact_level'] == 'Considerable') &
            (_fho_areas['forecast_period'] == forecast_period)
        ]
        
        fho_catastrophic = _fho_areas[
            (_fho_areas['valid_date'] == start_date) &
            issuance_time_mask(_fho_areas['issuance_time'], issuance_time) &
            (_fho_areas['impact_level'] == 'Catastrophic') &
            (_fho_areas['forecast_period'] == forecast_period)
        ]
        
        # Get Limited polygons for context
        fho_limited = _fho_areas[
            (_fho_areas['valid_date'] == start_date) &
            issuance_time_mask(_fho_areas['issuance_time'], issuance_time) &
            (_fho_areas['impact_level'] == 'Limited_merged') &
            (_fho_areas['forecast_period'] == forecast_period)
        ]
        
        # Get verification window
        verif_start, verif_end = get_date_range(issuance_time, forecast_period, start_date)
        
        if verif_start is not None and verif_end is not None:
            # Filter LSRs for verification window
            lsrs_valid = _lsrs[
                (_lsrs['VALID'] >= verif_start) &
                (_lsrs['VALID'] < verif_end)
            ]

            # Filter FFWs for verification window
            ffws_valid = _ffws[
                (_ffws['ISSUED'] <= verif_end) &
                (_ffws['EXPIRED'] >= verif_start)
            ]
            
            # Get all high-impact FFWs for display
            all_high_impact_ffws = ffws_valid[ffws_valid['DAMAGTAG'].isin(['CONSIDERABLE', 'CATASTROPHIC'])]
            
            # Get FFWs matching selected impact level for verification
            impact_level_ffws = ffws_valid[ffws_valid['DAMAGTAG'] == impact_level.upper()]
            
            # Get FFWs with no tag
            no_tag_ffws = ffws_valid[~ffws_valid['DAMAGTAG'].isin(['CONSIDERABLE', 'CATASTROPHIC'])]
            
            # Use the selected impact level's polygon for verification
            fho_filtered = fho_considerable if impact_level == 'Considerable' else fho_catastrophic
            
            if not fho_filtered.empty:
                merged_polygon = unary_union(fho_filtered.geometry)

                # Compute and store Considerable-only union for map display
                considerable_union_geo = None
                if not fho_considerable.empty:
                    con_poly = unary_union(fho_considerable.geometry)
                    considerable_union_geo = _safe_geo_interface(con_poly)

                catastrophic_union_geo = None
                if not fho_catastrophic.empty:
                    cat_polygon = unary_union(fho_catastrophic.geometry)
                    catastrophic_union_geo = _safe_geo_interface(cat_polygon)
                    # For verification, merge Catastrophic into Considerable coverage
                    if impact_level == 'Considerable':
                        merged_polygon = unary_union([merged_polygon, cat_polygon])

                # Vectorized hit/miss classification
                hit_mask = impact_level_ffws.intersects(merged_polygon)
                hits_gdf = impact_level_ffws[hit_mask]
                misses_gdf = impact_level_ffws[~hit_mask]

                # Other-impact FFWs via boolean filter
                other_impact_gdf = all_high_impact_ffws[all_high_impact_ffws['DAMAGTAG'] != impact_level.upper()]

                num_hits = len(hits_gdf)
                num_misses = len(misses_gdf)
                num_no_tag = len(no_tag_ffws)
                pod = num_hits / (num_hits + num_misses) if (num_hits + num_misses) > 0 else 0

                # Build limited features with type metadata
                limited_fc = {'type': 'FeatureCollection', 'features': []}
                if not fho_limited.empty:
                    ltd = fho_limited.copy()
                    ltd['type'] = 'Limited'
                    ltd['issuance_time'] = issuance_time
                    ltd['forecast_period'] = forecast_period
                    limited_fc = gdf_to_feature_collection(ltd)

                map_data = {
                    'fho_considerable': {
                        'type': 'Feature',
                        'geometry': considerable_union_geo,
                        'properties': {'type': 'Considerable'}
                    } if considerable_union_geo else None,
                    'fho_catastrophic': {
                        'type': 'Feature',
                        'geometry': catastrophic_union_geo,
                        'properties': {'type': 'Catastrophic'}
                    } if catastrophic_union_geo else None,
                    'limited': limited_fc,
                    'hits': gdf_to_feature_collection(hits_gdf, _ffw_popup),
                    'misses': gdf_to_feature_collection(misses_gdf, _ffw_popup),
                    'other_impact': gdf_to_feature_collection(other_impact_gdf, _ffw_popup),
                    'no_tag': gdf_to_feature_collection(no_tag_ffws, _ffw_popup),
                    'lsrs': gdf_to_feature_collection(lsrs_valid, _lsr_popup),
                }
            else:
                num_hits = 0
                num_misses = len(impact_level_ffws)
                num_no_tag = len(no_tag_ffws)
                pod = 0
                map_data = get_empty_ibw_geometries()
                map_data['misses'] = gdf_to_feature_collection(impact_level_ffws, _ffw_popup)
                if not fho_considerable.empty:
                    con_poly = unary_union(fho_considerable.geometry)
                    con_geo = _safe_geo_interface(con_poly)
                    if con_geo:
                        map_data['fho_considerable'] = {
                            'type': 'Feature',
                            'geometry': con_geo,
                            'properties': {'type': 'Considerable'}
                        }
                if not fho_catastrophic.empty:
                    cat_poly = unary_union(fho_catastrophic.geometry)
                    cat_geo = _safe_geo_interface(cat_poly)
                    if cat_geo:
                        map_data['fho_catastrophic'] = {
                            'type': 'Feature',
                            'geometry': cat_geo,
                            'properties': {'type': 'Catastrophic'}
                        }
                other_impact_gdf = all_high_impact_ffws[all_high_impact_ffws['DAMAGTAG'] != impact_level.upper()]
                map_data['other_impact'] = gdf_to_feature_collection(other_impact_gdf, _ffw_popup)
                map_data['no_tag'] = gdf_to_feature_collection(no_tag_ffws, _ffw_popup)
                map_data['lsrs'] = gdf_to_feature_collection(lsrs_valid, _lsr_popup)
                if not fho_limited.empty:
                    ltd = fho_limited.copy()
                    ltd['type'] = 'Limited'
                    ltd['issuance_time'] = issuance_time
                    ltd['forecast_period'] = forecast_period
                    map_data['limited'] = gdf_to_feature_collection(ltd)

            response = {
                'statistics': {
                    'capture_rate': pod,
                    'hits': num_hits,
                    'misses': num_misses,
                    'ffws_no_tag': num_no_tag,
                    # All FFW polygons in the verification window (FF/FL), independent of impact toggle
                    'total_ffws': len(ffws_valid),
                    'total_lsrs': len(lsrs_valid)
                },
                'geometries': map_data,
                'verification_window': {
                    'start': verif_start.isoformat(),
                    'end': verif_end.isoformat()
                }
            }

            _ibw_cache.put(ibw_cache_params, response)
            return jsonify(response)
        else:
            response = {
                'statistics': {
                    'capture_rate': 0, 'hits': 0, 'misses': 0,
                    'ffws_no_tag': 0, 'total_ffws': 0, 'total_lsrs': 0
                },
                'geometries': get_empty_ibw_geometries(),
                'verification_window': {}
            }
            _ibw_cache.put(ibw_cache_params, response)
            return jsonify(response)

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error processing IBW stats'}), 500

@app.route('/api/export-csv', methods=['POST'])
def export_csv():
    """Export current stats as a CSV file."""
    filters = request.get_json(silent=True)
    if not filters:
        return jsonify({'error': 'Request body is required'}), 400

    missing = [f for f in ('issuance_date', 'issuance', 'forecast_period') if not filters.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    # Snapshot data references
    _fho_areas, _lsrs, _ffws = _get_datasets()

    if _fho_areas is None or _lsrs is None or _ffws is None:
        return jsonify({'error': 'Data not loaded. Check GeoPackage files.'}), 500

    try:
        try:
            start_date = pd.to_datetime(filters['issuance_date']).date()
        except (ValueError, TypeError):
            return jsonify({'error': f'Invalid issuance_date: {filters["issuance_date"]}'}), 400
        end_date_raw = (filters.get('end_date') or '').strip()
        if end_date_raw:
            try:
                end_date = pd.to_datetime(end_date_raw).date()
            except (ValueError, TypeError):
                return jsonify({'error': f'Invalid end_date: {end_date_raw}'}), 400
        else:
            end_date = start_date
        forecast_period = filters['forecast_period']
        if forecast_period not in _PERIOD_OFFSETS:
            return jsonify({'error': f'Invalid forecast_period: {forecast_period}'}), 400
        if end_date < start_date:
            return jsonify({'error': 'End date cannot be before start date'}), 400
        if (end_date - start_date).days > 366:
            return jsonify({'error': 'Date range too large. Maximum range is 366 days.'}), 400
        issuance_time = normalize_issuance(filters['issuance'])

        issuance_filter = issuance_time_mask(_fho_areas['issuance_time'], issuance_time)
        impact_filter = (_fho_areas['impact_level'] == 'Limited_merged')
        period_filter = (_fho_areas['forecast_period'] == forecast_period)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['date', 'lsr_hits', 'lsr_misses', 'ffw_hits', 'ffw_misses', 'total_hits', 'total_misses', 'capture_rate', 'polygon_count'])

        available_dates = set(_fho_areas.loc[
            issuance_filter & impact_filter & period_filter, 'valid_date'
        ].unique())

        check_date = start_date
        while check_date <= end_date:
            lh = lm = fh = fm = 0
            poly_count = 0

            if check_date in available_dates:
                date_filter = (_fho_areas['valid_date'] == check_date)
                fho_for_date = _fho_areas[date_filter & issuance_filter & impact_filter & period_filter]
                poly_count = len(fho_for_date)

                verif_start, verif_end = get_date_range(issuance_time, forecast_period, check_date)
                if verif_start is not None and verif_end is not None and not fho_for_date.empty:
                    period_lsrs = _lsrs[(_lsrs['VALID'] >= verif_start) & (_lsrs['VALID'] < verif_end)]
                    period_ffws = _ffws[(_ffws['ISSUED'] <= verif_end) & (_ffws['EXPIRED'] >= verif_start)]
                    if not period_lsrs.empty:
                        lsr_j = gpd.sjoin(period_lsrs, fho_for_date, predicate='intersects', how='inner')
                        lh = lsr_j.index.nunique()
                        lm = len(period_lsrs) - lh
                    if not period_ffws.empty:
                        ffw_j = gpd.sjoin(period_ffws, fho_for_date, predicate='intersects', how='inner')
                        fh = ffw_j.index.nunique()
                        fm = len(period_ffws) - fh

            total_h = lh + fh
            total_m = lm + fm
            day_rate = round(total_h / (total_h + total_m), 4) if (total_h + total_m) > 0 else 0
            writer.writerow([check_date.strftime('%Y-%m-%d'), lh, lm, fh, fm, total_h, total_m, day_rate, poly_count])
            check_date += timedelta(days=1)

        output.seek(0)
        safe_date = start_date.strftime('%Y-%m-%d')
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename=fho_verification_{safe_date}.csv'}
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Internal server error exporting CSV'}), 500

@app.route('/api/high-impact-events')
def get_high_impact_events():
    """Get dates with Considerable/Catastrophic FHO polygons or FFWs (pre-computed at load)."""
    if _high_impact_events is None:
        return jsonify({'error': 'Data not loaded'}), 500
    return jsonify(_high_impact_events)

if __name__ == '__main__':
    app.run(debug=True) 