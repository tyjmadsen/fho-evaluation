from flask import Flask, render_template, jsonify, request
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import box
from datetime import datetime, timedelta
from shapely.ops import unary_union
import json

# Custom JSON encoder to handle NaN values
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer, np.floating)):
            if np.isnan(obj) or np.isinf(obj):
                return None
            return int(obj) if isinstance(obj, np.integer) else float(obj)
        return super().default(obj)

app = Flask(__name__)
app.json_encoder = CustomJSONEncoder

# Load data
print("Loading data...")
# Load FHO data from all years and issuances
fho_layers = []
for year in range(2022, 2026):
    for period in ['am', 'pm']:
        layer_name = f'fho_{year}_{period}'
        try:
            layer = gpd.read_file('fho_all.gpkg', layer=layer_name).to_crs("EPSG:4326")
            fho_layers.append(layer)
        except Exception as e:
            print(f"Could not read layer {layer_name}: {e}")

# Concatenate all data
fho_areas = pd.concat(fho_layers, ignore_index=True) if fho_layers else None

# Load verification data
print("Loading verification data...")
# Load LSR data
lsrs = gpd.read_file("LSRs_flood_allYears.gpkg").to_crs("EPSG:4326")

# Load flood warnings from all years
ffws = []
for year in range(2022, 2026):
    try:
        ffw = gpd.read_file("flood_warnings_all.gpkg", layer=f"wwa_{year}").to_crs("EPSG:4326")
        ffws.append(ffw)
    except Exception as e:
        print(f"Could not read flood warnings for {year}: {e}")

# Combine all flood warnings
ffws = pd.concat(ffws) if ffws else None

# Convert timestamps to datetime
lsrs["VALID"] = pd.to_datetime(lsrs["VALID"])
ffws["ISSUED"] = pd.to_datetime(ffws["ISSUED"])
ffws["EXPIRED"] = pd.to_datetime(ffws["EXPIRED"])

# Filter for flood warnings
ffws = ffws[ffws["PHENOM"] == "FF"]

def get_date_range(issuance_time, forecast_period, fho_issuance_date):
    """Get the date range for a given forecast period based on FHO issuance date."""
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
    
    # Adjust for AM/PM issuance
    if issuance_time.lower() == "am":
        # AM issuance at 8 AM EDT
        start_time = datetime.strptime("08:00:00", "%H:%M:%S").time()
        end_time = datetime.strptime("08:00:00", "%H:%M:%S").time()
    else:  # PM issuance
        # PM issuance at 6 PM EDT
        start_time = datetime.strptime("18:00:00", "%H:%M:%S").time()
        end_time = datetime.strptime("18:00:00", "%H:%M:%S").time()
    
    # Calculate start and end dates with exact times
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
        dates = sorted(pd.to_datetime(fho_areas['valid_start']).dt.date.unique())
        
        # Convert dates to string format YYYY-MM-DD
        date_strings = [d.strftime('%Y-%m-%d') for d in dates]
        
        return jsonify(date_strings)
    except Exception as e:
        return jsonify([]), 500

def calculate_pod_for_polygon(polygon, lsrs, ffws):
    """Calculate POD for a single polygon."""
    # Get verification data that intersects with this polygon
    intersecting_lsrs = lsrs[lsrs.intersects(polygon)]
    intersecting_ffws = ffws[ffws.intersects(polygon)]
    
    # Calculate total hits and total events
    total_hits = len(intersecting_lsrs) + len(intersecting_ffws)
    total_events = len(lsrs) + len(ffws)
    
    # Calculate POD
    return total_hits / total_events if total_events > 0 else 0

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
            (pd.to_datetime(fho_areas['valid_start']).dt.date >= start_date) &
            (pd.to_datetime(fho_areas['valid_start']).dt.date <= end_date)
        )
        range_fho = fho_areas[date_range_filter & period_filter & issuance_filter & impact_filter]

        # Initialize POD analysis variables
        total_polygons = len(range_fho)
        polygons_meeting_threshold = 0
        polygon_pods = []

        # Calculate POD for each polygon in the date range
        for _, polygon_row in range_fho.iterrows():
            verif_start, verif_end = get_date_range(issuance_time, forecast_period, pd.to_datetime(polygon_row['valid_start']).date())
            
            if verif_start and verif_end:
                # Get verification data for this polygon's time window
                period_lsrs = lsrs[
                    (lsrs['VALID'] >= verif_start) &
                    (lsrs['VALID'] < verif_end)
                ]
                
                period_ffws = ffws[
                    (ffws['ISSUED'] <= verif_end) &
                    (ffws['EXPIRED'] >= verif_start)
                ]
                
                # Calculate POD for this polygon
                pod = calculate_pod_for_polygon(polygon_row.geometry, period_lsrs, period_ffws)
                polygon_pods.append(pod)
                
                if pod >= pod_threshold:
                    polygons_meeting_threshold += 1

        # Calculate threshold percentage
        threshold_percentage = (polygons_meeting_threshold / total_polygons * 100) if total_polygons > 0 else 0

        # Add POD analysis to the response
        pod_analysis = {
            'polygons_meeting_threshold': polygons_meeting_threshold,
            'total_polygons': total_polygons,
            'threshold_percentage': threshold_percentage,
            'threshold_value': pod_threshold
        }

        # First, get the map data for the selected FHO Issuance date
        selected_date_filter = (pd.to_datetime(fho_areas['valid_start']).dt.date == start_date)
        selected_fho = fho_areas[selected_date_filter & period_filter & issuance_filter & impact_filter]
        
        if not selected_fho.empty:
            # Get verification window for selected date
            selected_verif_start, selected_verif_end = get_date_range(issuance_time, forecast_period, start_date)
            
            if selected_verif_start and selected_verif_end:
                # Merge FHO polygons for selected date
                selected_merged = unary_union(selected_fho.geometry)
                
                # Create a GeoDataFrame to ensure it's in WGS84
                merged_gdf = gpd.GeoDataFrame(geometry=[selected_merged], crs=fho_areas.crs)
                merged_gdf = merged_gdf.to_crs('EPSG:4326')
                selected_merged = merged_gdf.geometry.iloc[0]
                
                # Get verification data for selected date only
                selected_lsrs = lsrs[
                    (lsrs['VALID'] >= selected_verif_start) &
                    (lsrs['VALID'] < selected_verif_end)
                ]
                
                selected_ffws = ffws[
                    (ffws['ISSUED'] <= selected_verif_end) &
                    (ffws['EXPIRED'] >= selected_verif_start)
                ]
                
                # Identify hits and misses for map display
                map_lsrs_hit = selected_lsrs[selected_lsrs.intersects(selected_merged)]
                map_ffws_hit = selected_ffws[selected_ffws.intersects(selected_merged)]
                
                map_lsrs_miss = selected_lsrs[~selected_lsrs.index.isin(map_lsrs_hit.index)]
                map_ffws_miss = selected_ffws[~selected_ffws.index.isin(map_ffws_hit.index)]
                
                # Prepare map features
                map_data = {
                    'fho': {
                        'type': 'Feature',
                        'geometry': selected_merged.__geo_interface__,
                        'properties': {}
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

        # Initialize cumulative statistics
        daily_stats = []  # Store stats for each day for debugging
        total_lsr_hits = 0
        total_lsr_misses = 0
        total_ffw_hits = 0
        total_ffw_misses = 0

        # If no end date is selected, calculate the end date based on the forecast period
        if not filters.get('end_date'):
            if forecast_period == "1-3":
                end_date = start_date + timedelta(days=2)  # Days 1-3
            elif forecast_period == "4-7":
                end_date = start_date + timedelta(days=6)  # Days 4-7 (days 4,5,6,7)
                start_date = start_date + timedelta(days=3)  # Start from day 4
            elif forecast_period == "1-7":
                end_date = start_date + timedelta(days=6)  # Days 1-7
            else:
                end_date = start_date

        # Process each date in the range for statistics
        current_date = start_date
        while current_date <= end_date:
            # Filter FHO areas for current date and issuance time
            date_filter = (pd.to_datetime(fho_areas['valid_start']).dt.date == current_date)
            
            # Get all FHOs for this date and issuance time
            fho_for_date = fho_areas[date_filter & issuance_filter & impact_filter]
            
            if not fho_for_date.empty:
                # Filter for the specific forecast period
                fho_filtered = fho_for_date[period_filter]
                
                if not fho_filtered.empty:
                    # Get verification window for current date
                    verif_start, verif_end = get_date_range(issuance_time, forecast_period, current_date)
                    
                    if verif_start and verif_end:
                        # Merge FHO polygons for current date
                        merged_polygon = unary_union(fho_filtered.geometry)
                        
                        # Filter LSRs and FFWs for the verification window
                        lsrs_valid = lsrs[
                            (lsrs['VALID'] >= verif_start) &
                            (lsrs['VALID'] < verif_end)
                        ]
                        
                        ffws_valid = ffws[
                            (ffws['ISSUED'] <= verif_end) &
                            (ffws['EXPIRED'] >= verif_start)
                        ]
                        
                        # Identify hits and misses
                        lsrs_hit = lsrs_valid[lsrs_valid.intersects(merged_polygon)]
                        ffws_hit = ffws_valid[ffws_valid.intersects(merged_polygon)]
                        
                        lsrs_miss = lsrs_valid[~lsrs_valid.index.isin(lsrs_hit.index)]
                        ffws_miss = ffws_valid[~ffws_valid.index.isin(ffws_hit.index)]
                        
                        # Store daily statistics
                        daily_stats.append({
                            'date': current_date,
                            'lsr_hits': len(lsrs_hit),
                            'lsr_misses': len(lsrs_miss),
                            'ffw_hits': len(ffws_hit),
                            'ffw_misses': len(ffws_miss)
                        })
                        
                        # Add to cumulative statistics
                        total_lsr_hits += len(lsrs_hit)
                        total_lsr_misses += len(lsrs_miss)
                        total_ffw_hits += len(ffws_hit)
                        total_ffw_misses += len(ffws_miss)
                    else:
                        daily_stats.append({
                            'date': current_date,
                            'lsr_hits': 0,
                            'lsr_misses': 0,
                            'ffw_hits': 0,
                            'ffw_misses': 0
                        })
                else:
                    daily_stats.append({
                        'date': current_date,
                        'lsr_hits': 0,
                        'lsr_misses': 0,
                        'ffw_hits': 0,
                        'ffw_misses': 0
                    })
            else:
                daily_stats.append({
                    'date': current_date,
                    'lsr_hits': 0,
                    'lsr_misses': 0,
                    'ffw_hits': 0,
                    'ffw_misses': 0
                })
            
            current_date += timedelta(days=1)
        
        # Calculate cumulative statistics
        total_hits = total_lsr_hits + total_ffw_hits
        total_misses = total_lsr_misses + total_ffw_misses
        pod = total_hits / (total_hits + total_misses) if (total_hits + total_misses) > 0 else 0
        
        # Prepare response with cumulative statistics and selected date map data
        response = {
            'statistics': {
                'pod': pod,
                'total_hits': total_hits,
                'total_misses': total_misses,
                'lsr_hits': total_lsr_hits,
                'lsr_misses': total_lsr_misses,
                'ffw_hits': total_ffw_hits,
                'ffw_misses': total_ffw_misses,
                'total_days': len(daily_stats),
                'days_included': [stats['date'].strftime('%Y-%m-%d') for stats in daily_stats],
                'days_excluded': [current_date.strftime('%Y-%m-%d') for current_date in pd.date_range(start_date, end_date) 
                                if current_date.strftime('%Y-%m-%d') not in [stats['date'].strftime('%Y-%m-%d') for stats in daily_stats]]
            },
            'geometries': map_data,
            'pod_analysis': pod_analysis
        }
        
        return jsonify(response)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def get_empty_geometries():
    """Helper function to return empty geometry collections."""
    return {
        'fho': {'type': 'FeatureCollection', 'features': []},
        'lsrs_hit': {'type': 'FeatureCollection', 'features': []},
        'lsrs_miss': {'type': 'FeatureCollection', 'features': []},
        'ffws_hit': {'type': 'FeatureCollection', 'features': []},
        'ffws_miss': {'type': 'FeatureCollection', 'features': []}
    }

def row_to_feature(row):
    """Helper function to convert a GeoDataFrame row to a GeoJSON feature."""
    properties = row.drop('geometry').to_dict()
    # Convert NaN and special values to None
    properties = {k: None if isinstance(v, (float, int)) and (np.isnan(v) or np.isinf(v)) else v 
                 for k, v in properties.items()}
    
    # Enhance LSR popup content
    if 'EVENT' in properties and properties['EVENT'] is not None:
        popup_content = f"""
            <b>LSR Details:</b><br>
            Event: {properties['EVENT']}<br>
            Location: {properties.get('CITY', 'Unknown')}, {properties.get('STATE', 'Unknown')}<br>
            Time: {properties.get('VALID', 'Unknown')}<br>
            Source: {properties.get('SOURCE', 'Unknown')}<br>
            Remarks: {properties.get('REMARKS', 'None')}
        """
        properties['popup_content'] = popup_content
    # Enhance FFW popup content
    elif 'PHENOM' in properties and properties['PHENOM'] == 'FF':
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
            (pd.to_datetime(fho_areas['valid_start']).dt.date == start_date) &
            (fho_areas['issuance_time'].str.lower() == issuance_time) &
            (fho_areas['impact_level'] == 'Considerable') &
            (fho_areas['forecast_period'] == forecast_period)
        ]
        
        fho_catastrophic = fho_areas[
            (pd.to_datetime(fho_areas['valid_start']).dt.date == start_date) &
            (fho_areas['issuance_time'].str.lower() == issuance_time) &
            (fho_areas['impact_level'] == 'Catastrophic') &
            (fho_areas['forecast_period'] == forecast_period)
        ]
        
        # Get Limited polygons for context
        fho_limited = fho_areas[
            (pd.to_datetime(fho_areas['valid_start']).dt.date == start_date) &
            (fho_areas['issuance_time'].str.lower() == issuance_time) &
            (fho_areas['impact_level'] == 'Limited_merged') &
            (fho_areas['forecast_period'] == forecast_period)
        ]
        
        # Get verification window
        verif_start, verif_end = get_date_range(issuance_time, forecast_period, start_date)
        
        if verif_start and verif_end:
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
                # Merge FHO polygons
                merged_polygon = unary_union(fho_filtered.geometry)
                merged_gdf = gpd.GeoDataFrame(geometry=[merged_polygon], crs=fho_areas.crs)
                merged_gdf = merged_gdf.to_crs('EPSG:4326')
                merged_polygon = merged_gdf.geometry.iloc[0]

                # If we're looking at Considerable FFWs, also include Catastrophic FHO areas
                if impact_level == 'Considerable' and not fho_catastrophic.empty:
                    cat_polygon = unary_union(fho_catastrophic.geometry)
                    cat_gdf = gpd.GeoDataFrame(geometry=[cat_polygon], crs=fho_areas.crs)
                    cat_gdf = cat_gdf.to_crs('EPSG:4326')
                    cat_polygon = cat_gdf.geometry.iloc[0]
                    # Merge with Considerable polygon
                    merged_polygon = unary_union([merged_polygon, cat_polygon])
                
                # Calculate hits and misses for selected impact level
                for _, ffw in impact_level_ffws.iterrows():
                    if ffw.geometry.intersects(merged_polygon):
                        hits.append(ffw)
                    else:
                        misses.append(ffw)
                
                # Add other impact level FFWs to separate list
                for _, ffw in all_high_impact_ffws.iterrows():
                    if ffw['DAMAGTAG'] != impact_level.upper():
                        other_impact_ffws.append(ffw)
                
                # Calculate statistics
                num_hits = len(hits)
                num_misses = len(misses)
                num_no_tag = len(no_tag_ffws)
                pod = num_hits / (num_hits + num_misses) if (num_hits + num_misses) > 0 else 0
                
                # Prepare map features
                map_data = {
                    'fho_considerable': {
                        'type': 'Feature',
                        'geometry': unary_union(fho_considerable.geometry).__geo_interface__ if not fho_considerable.empty else None,
                        'properties': {'type': 'Considerable'}
                    },
                    'fho_catastrophic': {
                        'type': 'Feature',
                        'geometry': unary_union(fho_catastrophic.geometry).__geo_interface__ if not fho_catastrophic.empty else None,
                        'properties': {'type': 'Catastrophic'}
                    },
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
                    }
                }
            else:
                # If no FHO polygon, all high-impact FFWs are misses
                misses = all_high_impact_ffws
                num_hits = 0
                num_misses = len(misses)
                num_no_tag = len(no_tag_ffws)
                pod = 0
                map_data = get_empty_geometries()
            
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

@app.route('/api/high-impact-events')
def get_high_impact_events():
    """Get dates with Considerable/Catastrophic FHO polygons or FFWs."""
    try:
        # Get dates with Considerable FHO polygons
        considerable_dates = fho_areas[
            (fho_areas['impact_level'] == 'Considerable')
        ].apply(lambda x: {
            'date': pd.to_datetime(x['valid_start']).strftime('%Y-%m-%d'),
            'issuance': x['issuance_time'],
            'period': x['forecast_period']
        }, axis=1).tolist()

        # Get dates with Catastrophic FHO polygons
        catastrophic_dates = fho_areas[
            (fho_areas['impact_level'] == 'Catastrophic')
        ].apply(lambda x: {
            'date': pd.to_datetime(x['valid_start']).strftime('%Y-%m-%d'),
            'issuance': x['issuance_time'],
            'period': x['forecast_period']
        }, axis=1).tolist()

        # Get dates with high-impact FFWs but no corresponding FHO
        high_impact_ffws = ffws[ffws['DAMAGTAG'].isin(['CONSIDERABLE', 'CATASTROPHIC'])]
        ffw_dates = []
        
        for _, ffw in high_impact_ffws.iterrows():
            ffw_date = ffw['ISSUED'].strftime('%Y-%m-%d')
            # Check if there's no corresponding FHO polygon
            if not any(d['date'] == ffw_date for d in considerable_dates + catastrophic_dates):
                ffw_dates.append({
                    'date': ffw_date,
                    'tag': ffw['DAMAGTAG'],
                    'issued': ffw['ISSUED'].strftime('%H:%M:%S'),
                    'expired': ffw['EXPIRED'].strftime('%H:%M:%S')
                })

        return jsonify({
            'considerable_fho': considerable_dates,
            'catastrophic_fho': catastrophic_dates,
            'high_impact_ffws': ffw_dates
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True) 