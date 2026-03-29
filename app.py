from flask import Flask, render_template, jsonify, request, Response
import geopandas as gpd
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from shapely.ops import unary_union
from tqdm import tqdm
import os
import io
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)


class CustomJSONProvider(app.json_provider_class):
    def default(self, obj):
        if isinstance(obj, (np.integer, np.floating)):
            if np.isnan(obj) or np.isinf(obj):
                return None
            return int(obj) if isinstance(obj, np.integer) else float(obj)
        return super().default(obj)


app.json_provider_class = CustomJSONProvider
app.json = CustomJSONProvider(app)

# Cache for loaded data
DATA_CACHE = {}

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
    if 'fho_areas' in DATA_CACHE and 'lsrs' in DATA_CACHE and 'ffws' in DATA_CACHE:
        return DATA_CACHE['fho_areas'], DATA_CACHE['lsrs'], DATA_CACHE['ffws']

    print("Loading FHO data...")
    years = range(2022, 2027)
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
    if fho_areas is None:
        print("Warning: No FHO areas were loaded successfully")
        return None, None, None
    print(f"Loaded {len(fho_areas)} FHO areas")

    print("Loading LSR data...")
    try:
        lsrs = gpd.read_file("LSRs_flood_allYears.gpkg", layer="LSRs_flood_allYears").to_crs("EPSG:4326")
        print(f"Loaded {len(lsrs)} LSRs")
    except Exception as e:
        print(f"Could not read LSR data: {e}")
        return None, None, None

    print("Loading flood warnings...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        ffws = []
        futures = [executor.submit(load_warning_layer, year) for year in years]
        
        for future in tqdm(as_completed(futures), total=len(futures), desc="Loading flood warnings"):
            layer = future.result()
            if layer is not None:
                ffws.append(layer)

    print("Combining flood warnings...")
    ffws = pd.concat(ffws, ignore_index=True) if ffws else None
    if ffws is None:
        print("Warning: No flood warnings were loaded successfully")
        return None, None, None
    print(f"Loaded {len(ffws)} flood warnings")

    print("Processing timestamps...")
    lsrs["VALID"] = pd.to_datetime(lsrs["VALID"], errors="coerce")
    if lsrs["VALID"].dt.tz is not None:
        lsrs["VALID"] = lsrs["VALID"].dt.tz_localize(None)
    ffws["ISSUED"] = pd.to_datetime(ffws["ISSUED"], errors="coerce")
    if ffws["ISSUED"].dt.tz is not None:
        ffws["ISSUED"] = ffws["ISSUED"].dt.tz_localize(None)
    ffws["EXPIRED"] = pd.to_datetime(ffws["EXPIRED"], errors="coerce")
    if ffws["EXPIRED"].dt.tz is not None:
        ffws["EXPIRED"] = ffws["EXPIRED"].dt.tz_localize(None)
    ffws = ffws.dropna(subset=["ISSUED", "EXPIRED"])

    ffws = ffws[ffws["PHENOM"].isin(["FF", "FL"])]

    # ENH-2: Pre-parse dates at load time
    fho_areas['valid_start'] = pd.to_datetime(fho_areas['valid_start'])
    fho_areas['valid_date'] = fho_areas['valid_start'].dt.date

    # ENH-3: Build spatial index at load time
    _ = lsrs.sindex
    _ = ffws.sindex

    # Cache the results
    DATA_CACHE['fho_areas'] = fho_areas
    DATA_CACHE['lsrs'] = lsrs
    DATA_CACHE['ffws'] = ffws

    print("Data loading complete!")
    return fho_areas, lsrs, ffws

# Load data at startup
fho_areas, lsrs, ffws = load_data()

def get_date_range(issuance_time, forecast_period, fho_issuance_date):
    """Get the date range for a given forecast period based on FHO issuance date.
    
    All times are in UTC to match the FHO data and verification data.
    - AM issuance: 12:00 UTC (7 AM CDT / 6 AM CST)
    - PM issuance: 21:00 UTC (4 PM CDT / 3 PM CST)
    """
    if forecast_period == "1-3":
        start_days = 0  # Start from issuance day
        end_days = 3
    elif forecast_period == "4-7":
        start_days = 3  # Start from day 4
        end_days = 7
    elif forecast_period == "1-7":
        start_days = 0  # Start from issuance day
        end_days = 7
    else:
        return None, None
    
    # Use UTC times to match FHO data
    if issuance_time.lower() == "am":
        # AM issuance at 12:00 UTC (7 AM CDT / 6 AM CST)
        start_time = datetime.strptime("12:00:00", "%H:%M:%S").time()
        end_time = datetime.strptime("12:00:00", "%H:%M:%S").time()
    else:  # PM issuance
        # PM issuance at 21:00 UTC (4 PM CDT / 3 PM CST)
        start_time = datetime.strptime("21:00:00", "%H:%M:%S").time()
        end_time = datetime.strptime("21:00:00", "%H:%M:%S").time()
    
    # Calculate start and end dates with exact times in UTC
    start_date = datetime.combine(fho_issuance_date + timedelta(days=start_days), start_time)
    end_date = datetime.combine(fho_issuance_date + timedelta(days=end_days), end_time)
    
    return start_date, end_date

@app.route('/')
def index():
    return render_template('fho_evaluation.html')

@app.route('/api/available-dates', methods=['GET'])
def get_available_dates():
    """Get a list of dates where FHO data is available."""
    try:
        if fho_areas is None:
            return jsonify([])
        
        # Get unique dates from the valid_start column
        dates = sorted(fho_areas['valid_date'].unique())
        
        # Convert dates to string format YYYY-MM-DD
        date_strings = [d.strftime('%Y-%m-%d') for d in dates]
        
        return jsonify(date_strings)
    except Exception as e:
        return jsonify([]), 500

@app.route('/api/stats', methods=['POST'])
def get_stats():
    filters = request.json
    
    try:
        start_date = pd.to_datetime(filters['issuance_date']).date()
        # If end_date is empty or not provided, use the start_date
        end_date = pd.to_datetime(filters.get('end_date')).date() if filters.get('end_date') else start_date
        issuance = filters['issuance']
        forecast_period = filters['forecast_period']
        pod_threshold = float(filters.get('pod_threshold', 0.7))  # Default to 0.7 if not provided
        
        # Validate dates
        if end_date < start_date:
            return jsonify({
                'error': 'Invalid date range: End date cannot be before the FHO Issuance Date'
            }), 400
        
        # Convert issuance format
        issuance_time = 'am' if issuance == '00Z' else 'pm'
        
        # Define filters that will be used for both statistics and map data
        issuance_filter = (fho_areas['issuance_time'].str.lower() == issuance_time)
        impact_filter = (fho_areas['impact_level'] == 'Limited_merged')
        period_filter = (fho_areas['forecast_period'] == forecast_period)

        # Get all FHO areas in the date range
        date_range_filter = (
            (fho_areas['valid_date'] >= start_date) &
            (fho_areas['valid_date'] <= end_date)
        )
        range_fho = fho_areas[date_range_filter & period_filter & issuance_filter & impact_filter]

        min_events = 1
        total_polygons = len(range_fho)
        above_threshold = 0
        below_threshold = 0
        verified_polygon_indices = set()

        if not range_fho.empty:
            for vdate, group in range_fho.groupby('valid_date'):
                verif_start, verif_end = get_date_range(issuance_time, forecast_period, vdate)
                if not verif_start or not verif_end:
                    below_threshold += len(group)
                    continue

                period_lsrs = lsrs[
                    (lsrs['VALID'] >= verif_start) &
                    (lsrs['VALID'] < verif_end)
                ]
                period_ffws = ffws[
                    (ffws['ISSUED'] <= verif_end) &
                    (ffws['EXPIRED'] >= verif_start)
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

        threshold_percentage = (above_threshold / total_polygons * 100) if total_polygons > 0 else 0

        pod_analysis = {
            'above_threshold': above_threshold,
            'below_threshold': below_threshold,
            'total_polygons': total_polygons,
            'threshold_percentage': threshold_percentage,
            'threshold_value': min_events
        }

        # Get map data for the selected FHO issuance date
        orig_start = pd.to_datetime(filters['issuance_date']).date()
        selected_date_filter = (fho_areas['valid_date'] == orig_start)
        selected_fho = fho_areas[selected_date_filter & period_filter & issuance_filter & impact_filter]
        
        if not selected_fho.empty:
            selected_verif_start, selected_verif_end = get_date_range(issuance_time, forecast_period, orig_start)
            
            if selected_verif_start and selected_verif_end:
                selected_merged = unary_union(selected_fho.geometry)
                merged_gdf = gpd.GeoDataFrame(geometry=[selected_merged], crs=fho_areas.crs)
                merged_gdf = merged_gdf.to_crs('EPSG:4326')
                selected_merged = merged_gdf.geometry.iloc[0]
                
                selected_lsrs = lsrs[
                    (lsrs['VALID'] >= selected_verif_start) &
                    (lsrs['VALID'] < selected_verif_end)
                ]
                selected_ffws = ffws[
                    (ffws['ISSUED'] <= selected_verif_end) &
                    (ffws['EXPIRED'] >= selected_verif_start)
                ]
                
                map_lsrs_hit = selected_lsrs[selected_lsrs.intersects(selected_merged)]
                map_ffws_hit = selected_ffws[selected_ffws.intersects(selected_merged)]
                map_lsrs_miss = selected_lsrs[~selected_lsrs.index.isin(map_lsrs_hit.index)]
                map_ffws_miss = selected_ffws[~selected_ffws.index.isin(map_ffws_hit.index)]
                
                # Per-polygon verification via spatial join (vectorized)
                sel_wgs = selected_fho.to_crs('EPSG:4326').copy()

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

                poly_features = []
                for idx, row in sel_wgs.iterrows():
                    lsr_count = int(lsr_counts_per_poly.get(idx, 0))
                    ffw_count = int(ffw_counts_per_poly.get(idx, 0))
                    hit_count = lsr_count + ffw_count
                    poly_features.append({
                        'type': 'Feature',
                        'geometry': row.geometry.__geo_interface__,
                        'properties': {
                            'polygon_id': str(row.get('polygon_id', '')),
                            'area_sqkm': round(float(row.get('area_sqkm', 0)), 1),
                            'area_bin': str(row.get('area_bin', '')),
                            'impact_level': str(row.get('impact_level', '')),
                            'forecast_period': forecast_period,
                            'issuance_time': issuance_time.upper(),
                            'issuance_date': orig_start.strftime('%Y-%m-%d'),
                            'hit_count': hit_count,
                            'verified': hit_count >= 1,
                            'lsr_hits': lsr_count,
                            'ffw_hits': ffw_count,
                            'centroid_x': round(float(row.get('centroid_x', 0)), 4) if 'centroid_x' in row.index else None,
                            'centroid_y': round(float(row.get('centroid_y', 0)), 4) if 'centroid_y' in row.index else None
                        }
                    })

                map_data = {
                    'fho': {
                        'type': 'Feature',
                        'geometry': selected_merged.__geo_interface__,
                        'properties': {}
                    },
                    'fho_polygons': {
                        'type': 'FeatureCollection',
                        'features': poly_features
                    },
                    'lsrs_hit': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for _, row in map_lsrs_hit.iterrows()]
                    },
                    'lsrs_miss': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for _, row in map_lsrs_miss.iterrows()]
                    },
                    'ffws_hit': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for _, row in map_ffws_hit.iterrows()]
                    },
                    'ffws_miss': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for _, row in map_ffws_miss.iterrows()]
                    }
                }
            else:
                map_data = get_empty_geometries()
        else:
            map_data = get_empty_geometries()

        # Track unique event indices across overlapping verification windows
        daily_stats = []
        seen_lsr_hits = set()
        seen_lsr_misses = set()
        seen_ffw_hits = set()
        seen_ffw_misses = set()

        if not filters.get('end_date'):
            if forecast_period == "1-3":
                end_date = start_date + timedelta(days=2)
            elif forecast_period == "4-7":
                end_date = start_date + timedelta(days=6)
                start_date = start_date + timedelta(days=3)
            elif forecast_period == "1-7":
                end_date = start_date + timedelta(days=6)
            else:
                end_date = start_date

        check_date = start_date
        while check_date <= end_date:
            date_filter = (fho_areas['valid_date'] == check_date)
            fho_for_date = fho_areas[date_filter & issuance_filter & impact_filter]

            if not fho_for_date.empty:
                fho_filtered = fho_for_date[period_filter]

                if not fho_filtered.empty:
                    verif_start, verif_end = get_date_range(issuance_time, forecast_period, check_date)

                    if verif_start and verif_end:
                        merged_polygon = unary_union(fho_filtered.geometry)

                        lsrs_valid = lsrs[
                            (lsrs['VALID'] >= verif_start) &
                            (lsrs['VALID'] < verif_end)
                        ]
                        ffws_valid = ffws[
                            (ffws['ISSUED'] <= verif_end) &
                            (ffws['EXPIRED'] >= verif_start)
                        ]

                        lsrs_hit = lsrs_valid[lsrs_valid.intersects(merged_polygon)]
                        ffws_hit = ffws_valid[ffws_valid.intersects(merged_polygon)]
                        lsrs_miss = lsrs_valid[~lsrs_valid.index.isin(lsrs_hit.index)]
                        ffws_miss = ffws_valid[~ffws_valid.index.isin(ffws_hit.index)]

                        seen_lsr_hits.update(lsrs_hit.index.tolist())
                        seen_ffw_hits.update(ffws_hit.index.tolist())
                        seen_lsr_misses.update(lsrs_miss.index.tolist())
                        seen_ffw_misses.update(ffws_miss.index.tolist())

                        daily_stats.append({
                            'date': check_date,
                            'lsr_hits': len(lsrs_hit),
                            'lsr_misses': len(lsrs_miss),
                            'ffw_hits': len(ffws_hit),
                            'ffw_misses': len(ffws_miss)
                        })
                    else:
                        daily_stats.append({'date': check_date, 'lsr_hits': 0, 'lsr_misses': 0, 'ffw_hits': 0, 'ffw_misses': 0})
                else:
                    daily_stats.append({'date': check_date, 'lsr_hits': 0, 'lsr_misses': 0, 'ffw_hits': 0, 'ffw_misses': 0})
            else:
                daily_stats.append({'date': check_date, 'lsr_hits': 0, 'lsr_misses': 0, 'ffw_hits': 0, 'ffw_misses': 0})

            check_date += timedelta(days=1)

        # Events that were hit in any window are hits; remove them from misses
        seen_lsr_misses -= seen_lsr_hits
        seen_ffw_misses -= seen_ffw_hits

        total_lsr_hits = len(seen_lsr_hits)
        total_lsr_misses = len(seen_lsr_misses)
        total_ffw_hits = len(seen_ffw_hits)
        total_ffw_misses = len(seen_ffw_misses)
        total_hits = total_lsr_hits + total_ffw_hits
        total_misses = total_lsr_misses + total_ffw_misses
        pod = total_hits / (total_hits + total_misses) if (total_hits + total_misses) > 0 else 0
        
        # Compute FAR: fraction of total FHO area with no events
        total_fho_area = float(range_fho['area_sqkm'].sum()) if 'area_sqkm' in range_fho.columns and not range_fho.empty else 0

        if total_fho_area > 0 and 'area_sqkm' in range_fho.columns:
            verified_area = float(range_fho.loc[range_fho.index.isin(verified_polygon_indices), 'area_sqkm'].sum())
            false_alarm_area = total_fho_area - verified_area
            far = false_alarm_area / total_fho_area if total_fho_area > 0 else 0
        else:
            far = 0
            false_alarm_area = 0
            verified_area = 0

        # CSI (Critical Success Index): hits / (hits + misses + false_alarms)
        # hits = events inside FHO, misses = events outside, false_alarms = unverified polygons
        csi_denom = total_hits + total_misses + below_threshold
        csi = total_hits / csi_denom if csi_denom > 0 else 0

        # Area-bin stratified POD
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

        # Daily time series
        daily_series = []
        for ds in daily_stats:
            day_hits = ds['lsr_hits'] + ds['ffw_hits']
            day_total = day_hits + ds['lsr_misses'] + ds['ffw_misses']
            daily_series.append({
                'date': ds['date'].strftime('%Y-%m-%d'),
                'lsr_hits': ds['lsr_hits'],
                'lsr_misses': ds['lsr_misses'],
                'ffw_hits': ds['ffw_hits'],
                'ffw_misses': ds['ffw_misses'],
                'pod': round(day_hits / day_total * 100, 1) if day_total > 0 else 0
            })

        sel_vs, sel_ve = get_date_range(issuance_time, forecast_period, pd.to_datetime(filters['issuance_date']).date())
        verif_window = {'start': sel_vs.isoformat(), 'end': sel_ve.isoformat()} if sel_vs and sel_ve else {}

        days_included_set = {stats['date'].strftime('%Y-%m-%d') for stats in daily_stats}

        response = {
            'statistics': {
                'pod': pod,
                'far': round(far, 4),
                'csi': round(csi, 4),
                'total_hits': total_hits,
                'total_misses': total_misses,
                'lsr_hits': total_lsr_hits,
                'lsr_misses': total_lsr_misses,
                'ffw_hits': total_ffw_hits,
                'ffw_misses': total_ffw_misses,
                'total_days': len(daily_stats),
                'total_fho_area_sqkm': round(total_fho_area, 1),
                'verified_area_sqkm': round(verified_area, 1),
                'days_included': [stats['date'].strftime('%Y-%m-%d') for stats in daily_stats],
                'days_excluded': [d.strftime('%Y-%m-%d') for d in pd.date_range(start_date, end_date) 
                                if d.strftime('%Y-%m-%d') not in days_included_set]
            },
            'daily_series': daily_series,
            'area_bin_stats': area_bin_stats,
            'geometries': map_data,
            'pod_analysis': pod_analysis,
            'verification_window': verif_window
        }
        
        return jsonify(response)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def get_empty_geometries():
    """Return empty geometry collections for FHO verification page."""
    return {
        'fho': {'type': 'FeatureCollection', 'features': []},
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

def row_to_feature(row):
    """Helper function to convert a GeoDataFrame row to a GeoJSON feature."""
    properties = row.drop('geometry').to_dict()
    # Convert NaN and special values to None
    properties = {k: None if isinstance(v, (float, int)) and (np.isnan(v) or np.isinf(v)) else v 
                 for k, v in properties.items()}
    
    # Enhance LSR popup content — tolerate both EVENT/TYPETEXT and REMARKS/REMARK
    if ('EVENT' in properties and properties['EVENT'] is not None) or ('TYPETEXT' in properties and properties['TYPETEXT'] is not None):
        event_type = properties.get('EVENT') or properties.get('TYPETEXT', 'Unknown')
        remarks = properties.get('REMARKS') or properties.get('REMARK', 'None')
        popup_content = f"""
            <b>LSR Details:</b><br>
            Event: {event_type}<br>
            Location: {properties.get('CITY', 'Unknown')}, {properties.get('STATE', 'Unknown')}<br>
            Time: {properties.get('VALID', 'Unknown')}<br>
            Source: {properties.get('SOURCE', 'Unknown')}<br>
            Remarks: {remarks}
        """
        properties['popup_content'] = popup_content
    # Enhance FFW popup content
    elif 'PHENOM' in properties and properties['PHENOM'] in ('FF', 'FL'):
        popup_content = f"""
            <b>Flood Warning Details:</b><br>
            Issued: {properties.get('ISSUED', 'Unknown')}<br>
            Expired: {properties.get('EXPIRED', 'Unknown')}<br>
            Phenomena: {properties.get('PHENOM', 'Unknown')}<br>
            Impact: {properties.get('DAMAGTAG', 'Unknown')}
        """
        properties['popup_content'] = popup_content
    
    return {
        'type': 'Feature',
        'geometry': row.geometry.__geo_interface__,
        'properties': properties
    }

@app.route('/ibw-validation')
def ibw_validation():
    return render_template('ibw_validation.html')

@app.route('/api/ibw-stats', methods=['POST'])
def get_ibw_stats():
    filters = request.json
    
    try:
        start_date = pd.to_datetime(filters['issuance_date']).date()
        issuance = filters['issuance']
        forecast_period = filters['forecast_period']
        impact_level = filters.get('impact_level', 'Considerable')  # Default to Considerable
        
        # Convert issuance format
        issuance_time = issuance.lower()
        
        # Filter FHO polygons
        fho_considerable = fho_areas[
            (fho_areas['valid_date'] == start_date) &
            (fho_areas['issuance_time'].str.lower() == issuance_time) &
            (fho_areas['impact_level'] == 'Considerable') &
            (fho_areas['forecast_period'] == forecast_period)
        ]
        
        fho_catastrophic = fho_areas[
            (fho_areas['valid_date'] == start_date) &
            (fho_areas['issuance_time'].str.lower() == issuance_time) &
            (fho_areas['impact_level'] == 'Catastrophic') &
            (fho_areas['forecast_period'] == forecast_period)
        ]
        
        # Get Limited polygons for context
        fho_limited = fho_areas[
            (fho_areas['valid_date'] == start_date) &
            (fho_areas['issuance_time'].str.lower() == issuance_time) &
            (fho_areas['impact_level'] == 'Limited_merged') &
            (fho_areas['forecast_period'] == forecast_period)
        ]
        
        # Get verification window
        verif_start, verif_end = get_date_range(issuance_time, forecast_period, start_date)
        
        if verif_start and verif_end:
            # Filter LSRs for verification window
            lsrs_valid = lsrs[
                (lsrs['VALID'] >= verif_start) &
                (lsrs['VALID'] < verif_end)
            ]

            # Filter FFWs for verification window
            ffws_valid = ffws[
                (ffws['ISSUED'] <= verif_end) &
                (ffws['EXPIRED'] >= verif_start)
            ]
            
            # Get all high-impact FFWs for display
            all_high_impact_ffws = ffws_valid[ffws_valid['DAMAGTAG'].isin(['CONSIDERABLE', 'CATASTROPHIC'])]
            
            # Get FFWs matching selected impact level for verification
            impact_level_ffws = ffws_valid[ffws_valid['DAMAGTAG'] == impact_level.upper()]
            
            # Get FFWs with no tag
            no_tag_ffws = ffws_valid[~ffws_valid['DAMAGTAG'].isin(['CONSIDERABLE', 'CATASTROPHIC'])]
            
            # Initialize hits and misses
            hits = []
            misses = []
            other_impact_ffws = []  # For FFWs of different impact level
            
            # Use the selected impact level's polygon for verification
            fho_filtered = fho_considerable if impact_level == 'Considerable' else fho_catastrophic
            
            if not fho_filtered.empty:
                merged_polygon = unary_union(fho_filtered.geometry)
                merged_gdf = gpd.GeoDataFrame(geometry=[merged_polygon], crs=fho_areas.crs)
                merged_gdf = merged_gdf.to_crs('EPSG:4326')
                merged_polygon = merged_gdf.geometry.iloc[0]

                # Compute and store Considerable-only union for map display
                considerable_union_geo = None
                if not fho_considerable.empty:
                    con_poly = unary_union(fho_considerable.geometry)
                    con_gdf = gpd.GeoDataFrame(geometry=[con_poly], crs=fho_areas.crs).to_crs('EPSG:4326')
                    considerable_union_geo = con_gdf.geometry.iloc[0].__geo_interface__

                catastrophic_union_geo = None
                if not fho_catastrophic.empty:
                    cat_polygon = unary_union(fho_catastrophic.geometry)
                    cat_gdf = gpd.GeoDataFrame(geometry=[cat_polygon], crs=fho_areas.crs).to_crs('EPSG:4326')
                    cat_polygon = cat_gdf.geometry.iloc[0]
                    catastrophic_union_geo = cat_polygon.__geo_interface__
                    # For verification, merge Catastrophic into Considerable coverage
                    if impact_level == 'Considerable':
                        merged_polygon = unary_union([merged_polygon, cat_polygon])

                for _, ffw in impact_level_ffws.iterrows():
                    if ffw.geometry.intersects(merged_polygon):
                        hits.append(ffw)
                    else:
                        misses.append(ffw)

                for _, ffw in all_high_impact_ffws.iterrows():
                    if ffw['DAMAGTAG'] != impact_level.upper():
                        other_impact_ffws.append(ffw)

                num_hits = len(hits)
                num_misses = len(misses)
                num_no_tag = len(no_tag_ffws)
                pod = num_hits / (num_hits + num_misses) if (num_hits + num_misses) > 0 else 0

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
                    'limited': {
                        'type': 'FeatureCollection',
                        'features': [{'type': 'Feature', 
                                    'geometry': geom.__geo_interface__, 
                                    'properties': {
                                        'type': 'Limited',
                                        'issuance_time': issuance_time,
                                        'forecast_period': forecast_period
                                    }} 
                                   for geom in fho_limited.geometry] if not fho_limited.empty else []
                    },
                    'hits': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for row in hits]
                    },
                    'misses': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for row in misses]
                    },
                    'other_impact': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for row in other_impact_ffws]
                    },
                    'no_tag': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for _, row in no_tag_ffws.iterrows()]
                    },
                    'lsrs': {
                        'type': 'FeatureCollection',
                        'features': [row_to_feature(row) for _, row in lsrs_valid.iterrows()]
                    }
                }
            else:
                num_hits = 0
                num_misses = len(all_high_impact_ffws)
                num_no_tag = len(no_tag_ffws)
                pod = 0
                map_data = get_empty_ibw_geometries()
                map_data['misses'] = {
                    'type': 'FeatureCollection',
                    'features': [row_to_feature(row) for _, row in all_high_impact_ffws.iterrows()]
                }
                map_data['no_tag'] = {
                    'type': 'FeatureCollection',
                    'features': [row_to_feature(row) for _, row in no_tag_ffws.iterrows()]
                }
                map_data['lsrs'] = {
                    'type': 'FeatureCollection',
                    'features': [row_to_feature(row) for _, row in lsrs_valid.iterrows()]
                }
            
            response = {
                'statistics': {
                    'pod': pod,
                    'hits': num_hits,
                    'misses': num_misses,
                    'ffws_no_tag': num_no_tag,
                    'total_ffws': num_hits + num_misses + num_no_tag
                },
                'geometries': map_data,
                'verification_window': {
                    'start': verif_start.isoformat(),
                    'end': verif_end.isoformat()
                }
            }
            
            return jsonify(response)
        else:
            return jsonify({'error': 'Invalid verification window'}), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export-csv', methods=['POST'])
def export_csv():
    """Export current stats as a CSV file."""
    filters = request.json
    try:
        start_date = pd.to_datetime(filters['issuance_date']).date()
        end_date = pd.to_datetime(filters.get('end_date')).date() if filters.get('end_date') else start_date
        issuance = filters['issuance']
        forecast_period = filters['forecast_period']
        issuance_time = 'am' if issuance == '00Z' else 'pm'

        issuance_filter = (fho_areas['issuance_time'].str.lower() == issuance_time)
        impact_filter = (fho_areas['impact_level'] == 'Limited_merged')
        period_filter = (fho_areas['forecast_period'] == forecast_period)

        if not filters.get('end_date'):
            if forecast_period == "1-3":
                end_date = start_date + timedelta(days=2)
            elif forecast_period == "4-7":
                end_date = start_date + timedelta(days=6)
                start_date = start_date + timedelta(days=3)
            elif forecast_period == "1-7":
                end_date = start_date + timedelta(days=6)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['date', 'lsr_hits', 'lsr_misses', 'ffw_hits', 'ffw_misses', 'total_hits', 'total_misses', 'pod', 'polygon_count'])

        check_date = start_date
        while check_date <= end_date:
            date_filter = (fho_areas['valid_date'] == check_date)
            fho_for_date = fho_areas[date_filter & issuance_filter & impact_filter & period_filter]
            poly_count = len(fho_for_date)

            verif_start, verif_end = get_date_range(issuance_time, forecast_period, check_date)
            lh = lm = fh = fm = 0
            if verif_start and verif_end and not fho_for_date.empty:
                merged = unary_union(fho_for_date.geometry)
                period_lsrs = lsrs[(lsrs['VALID'] >= verif_start) & (lsrs['VALID'] < verif_end)]
                period_ffws = ffws[(ffws['ISSUED'] <= verif_end) & (ffws['EXPIRED'] >= verif_start)]
                if not period_lsrs.empty:
                    mask = period_lsrs.intersects(merged)
                    lh = int(mask.sum())
                    lm = int((~mask).sum())
                if not period_ffws.empty:
                    mask = period_ffws.intersects(merged)
                    fh = int(mask.sum())
                    fm = int((~mask).sum())

            total_h = lh + fh
            total_m = lm + fm
            day_pod = round(total_h / (total_h + total_m), 4) if (total_h + total_m) > 0 else 0
            writer.writerow([check_date.strftime('%Y-%m-%d'), lh, lm, fh, fm, total_h, total_m, day_pod, poly_count])
            check_date += timedelta(days=1)

        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename=fho_verification_{filters["issuance_date"]}.csv'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/high-impact-events')
def get_high_impact_events():
    """Get dates with Considerable/Catastrophic FHO polygons or FFWs."""
    try:
        considerable_fho = fho_areas[fho_areas['impact_level'] == 'Considerable'].copy()
        considerable_fho['date'] = considerable_fho['valid_start'].dt.strftime('%Y-%m-%d')
        considerable_unique = considerable_fho[['date', 'issuance_time', 'forecast_period']].drop_duplicates()
        considerable_dates = considerable_unique.apply(lambda x: {
            'date': x['date'],
            'issuance': x['issuance_time'],
            'period': x['forecast_period']
        }, axis=1).tolist()

        catastrophic_fho = fho_areas[fho_areas['impact_level'] == 'Catastrophic'].copy()
        catastrophic_fho['date'] = catastrophic_fho['valid_start'].dt.strftime('%Y-%m-%d')
        catastrophic_unique = catastrophic_fho[['date', 'issuance_time', 'forecast_period']].drop_duplicates()
        catastrophic_dates = catastrophic_unique.apply(lambda x: {
            'date': x['date'],
            'issuance': x['issuance_time'],
            'period': x['forecast_period']
        }, axis=1).tolist()

        high_impact_ffws = ffws[ffws['DAMAGTAG'].isin(['CONSIDERABLE', 'CATASTROPHIC'])].copy()
        fho_date_set = set(d['date'] for d in considerable_dates + catastrophic_dates)
        high_impact_ffws['_ffw_date'] = high_impact_ffws['ISSUED'].dt.strftime('%Y-%m-%d')
        no_fho_ffws = high_impact_ffws[~high_impact_ffws['_ffw_date'].isin(fho_date_set)]
        ffw_dates = no_fho_ffws.apply(lambda r: {
            'date': r['_ffw_date'],
            'tag': r['DAMAGTAG'],
            'issued': r['ISSUED'].strftime('%H:%M:%S'),
            'expired': r['EXPIRED'].strftime('%H:%M:%S')
        }, axis=1).tolist() if not no_fho_ffws.empty else []

        return jsonify({
            'considerable_fho': considerable_dates,
            'catastrophic_fho': catastrophic_dates,
            'high_impact_ffws': ffw_dates
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True) 