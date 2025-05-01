// Configuration object for styles and constants
const config = {
    bounds: {
        CONUS: [
            [24.396308, -125.000000],
            [49.384358, -66.934570]
        ]
    },
    styles: {
        fho: {
            color: '#000000',
            weight: 2,
            opacity: 0.8,
            fillOpacity: 0.2
        },
        lsrsHit: {
            color: '#1a9850',
            weight: 2,
            opacity: 0.8,
            fillOpacity: 0.5
        },
        lsrsMiss: {
            color: '#cc0000',
            weight: 2,
            opacity: 0.8,
            fillOpacity: 0.5
        },
        ffwsHit: {
            color: '#4575b4',
            weight: 2,
            opacity: 0.8,
            fillOpacity: 0.5
        },
        ffwsMiss: {
            color: '#ff9900',
            weight: 2,
            opacity: 0.9,
            fillOpacity: 0.6
        },
        ffwsCatastrophic: {
            color: '#d73027',
            weight: 3,
            opacity: 0.9,
            fillOpacity: 0.7
        },
        ffwsConsiderable: {
            color: '#f46d43',
            weight: 3,
            opacity: 0.9,
            fillOpacity: 0.7
        }
    },
    pointMarkers: {
        radius: 5,
        weight: 1,
        opacity: 1,
        fillOpacity: 0.8
    }
};

// Initialize map with CONUS extent
const map = L.map('map');
map.fitBounds(config.bounds.CONUS);

// Create the base layers
const lightBaseMap = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 20,
    minZoom: 0
}).addTo(map);

const satelliteLayer = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community',
    maxZoom: 20,
    minZoom: 0
});

// Define base maps for layer control
const baseMaps = {
    "Light": lightBaseMap,
    "Satellite": satelliteLayer
};

// Add layer control
L.control.layers(baseMaps, null, {
    position: 'bottomright'
}).addTo(map);

// Add zoom control to a better position
map.zoomControl.setPosition('bottomright');

// Layer groups
const layers = {
    fho: L.layerGroup(),
    lsrsHit: L.layerGroup(),
    lsrsMiss: L.layerGroup(),
    ffwsHit: L.layerGroup(),
    ffwsMiss: L.layerGroup()
};

Object.values(layers).forEach(layer => layer.addTo(map));

// Enhanced cache management
const statsCache = new Map();
const MAX_CACHE_SIZE = 50; // Maximum number of cached results

// Helper function to manage cache size
function manageCacheSize() {
    if (statsCache.size > MAX_CACHE_SIZE) {
        // Remove oldest entry
        const oldestKey = statsCache.keys().next().value;
        statsCache.delete(oldestKey);
    }
}

// Helper function to generate cache key
function generateCacheKey(filters) {
    return `${filters.issuance_date}_${filters.end_date}_${filters.issuance}_${filters.forecast_period}_${filters.pod_threshold}`;
}

// Helper function to update loading states
function setLoadingState(isLoading, elementId = null) {
    if (elementId) {
        const element = document.getElementById(elementId);
        if (element) {
            element.classList.toggle('loading', isLoading);
        }
    } else {
        document.querySelector('.loading-overlay').style.display = isLoading ? 'flex' : 'none';
    }
}

// Helper function to create LSR markers
function createLSRMarker(feature, latlng, isHit) {
    if (isHit) {
        return L.circleMarker(latlng, {
            ...config.pointMarkers,
            fillColor: config.styles.lsrsHit.color,
            color: "#000"
        }).bindPopup(createPopupContent('LSR', feature, isHit));
    } else {
        // For misses, use a custom divIcon to create an X
        return L.marker(latlng, {
            icon: L.divIcon({
                html: '✕',
                className: 'lsr-miss-marker',
                iconSize: [12, 12]
            })
        }).bindPopup(createPopupContent('LSR', feature, isHit));
    }
}

// Helper function to create FFW layers
function createFFWLayer(features, isHit) {
    return L.geoJSON(features, {
        style: (feature) => getFFWStyle(feature, isHit),
        onEachFeature: (feature, layer) => {
            layer.bindPopup(createPopupContent('FFW', feature, isHit));
        }
    });
}

// Helper function to create FHO layer
function createFHOLayer(feature) {
    return L.geoJSON(feature, { 
        style: config.styles.fho,
        onEachFeature: (feature, layer) => {
            layer.bindPopup(createPopupContent('FHO', feature));
        }
    });
}

// Helper function to update statistics with loading states
function updateStatistics(stats) {
    const statsElements = {
        pod: { value: (stats.pod * 100).toFixed(1) + '%', loading: false },
        totalHits: { value: stats.total_hits, loading: false },
        totalMisses: { value: stats.total_misses, loading: false },
        lsrHits: { value: stats.lsr_hits, loading: false },
        lsrMisses: { value: stats.lsr_misses, loading: false },
        ffwHits: { value: stats.ffw_hits, loading: false },
        ffwMisses: { value: stats.ffw_misses, loading: false },
        totalDays: { value: stats.total_days, loading: false },
        daysIncluded: { value: stats.days_included.join(', '), loading: false }
    };

    // Update each element with loading state
    Object.entries(statsElements).forEach(([id, { value, loading }]) => {
        const element = document.getElementById(id);
        if (element) {
            element.classList.toggle('loading', loading);
            element.textContent = value;
        }
    });
}

// Function to update POD threshold statistics
function updatePodThresholdStats(data) {
    if (!data.pod_analysis) return;

    const {
        polygons_meeting_threshold,
        total_polygons,
        threshold_percentage
    } = data.pod_analysis;

    // Update the progress bar and percentage
    const progressBar = document.getElementById('podThresholdProgress');
    progressBar.style.width = `${threshold_percentage}%`;
    progressBar.setAttribute('aria-valuenow', threshold_percentage);

    // Update the percentage badge
    document.getElementById('podThresholdPercent').textContent = `${threshold_percentage.toFixed(1)}%`;

    // Update counts
    document.getElementById('podThresholdCount').textContent = polygons_meeting_threshold;
    document.getElementById('podTotalCount').textContent = total_polygons;
}

// Helper function to create popup content
function createPopupContent(type, feature, isHit = null) {
    switch (type) {
        case 'FHO':
            const issuanceTime = document.getElementById('issuance').value === '00Z' ? 'AM' : 'PM';
            const forecastPeriod = document.getElementById('forecastPeriod').value;
            const issuanceDate = document.getElementById('issuanceDate').value;
            return `
                <b>FHO Forecast Area</b><br>
                Issuance Date: ${issuanceDate}<br>
                Issuance Time: ${issuanceTime}<br>
                Forecast Period: Days ${forecastPeriod}<br>
                Impact Level: Limited
            `;
        case 'LSR':
            return feature.properties.popup_content || 
                `LSR ${isHit ? 'Hit' : 'Miss'}: ${feature.properties.EVENT || 'Unknown'}<br>Time: ${feature.properties.VALID || 'Unknown'}`;
        case 'FFW':
            return feature.properties.popup_content || 
                `<b>Flash Flood Warning Details:</b><br>
                Status: ${isHit ? 'Hit' : 'Miss'}<br>
                Issued: ${feature.properties.ISSUED || 'Unknown'}<br>
                Expired: ${feature.properties.EXPIRED || 'Unknown'}<br>
                Impact: ${feature.properties.DAMAGTAG || 'Unknown'}`;
        default:
            return '';
    }
}

// Helper function to get style for FFW based on damage tag
function getFFWStyle(feature, isHit) {
    if (feature.properties.DAMAGTAG === 'CATASTROPHIC') {
        return config.styles.ffwsCatastrophic;
    } else if (feature.properties.DAMAGTAG === 'CONSIDERABLE') {
        return config.styles.ffwsConsiderable;
    }
    return isHit ? config.styles.ffwsHit : config.styles.ffwsMiss;
}

// Helper function to add layer to map and update bounds
function addLayerToMap(layer, layerGroup, currentBounds) {
    layer.addTo(layerGroup);
    if (layer.getBounds().isValid()) {
        return currentBounds ? currentBounds.extend(layer.getBounds()) : layer.getBounds();
    }
    return currentBounds;
}

// Enhanced layer creation helper
function createAndAddLayer(features, layerType, isHit = null, layerGroup, currentBounds) {
    if (!features?.features?.length && !features?.geometry) return currentBounds;

    let layer;
    switch (layerType) {
        case 'LSR':
            layer = L.geoJSON(features, {
                pointToLayer: (feature, latlng) => createLSRMarker(feature, latlng, isHit)
            });
            break;
        case 'FFW':
            layer = createFFWLayer(features, isHit);
            break;
        case 'FHO':
            layer = createFHOLayer(features);
            break;
        default:
            return currentBounds;
    }

    return addLayerToMap(layer, layerGroup, currentBounds);
}

// Enhanced error handling
const ErrorHandler = {
    handleError: (error, fallbackAction) => {
        console.error("Error:", error);
        if (fallbackAction) fallbackAction();
    },
    showError: (message) => {
        // Create or update error notification
        let errorDiv = document.getElementById('error-notification');
        if (!errorDiv) {
            errorDiv = document.createElement('div');
            errorDiv.id = 'error-notification';
            errorDiv.className = 'alert alert-danger alert-dismissible fade show';
            errorDiv.style.position = 'fixed';
            errorDiv.style.top = '20px';
            errorDiv.style.right = '20px';
            errorDiv.style.zIndex = '9999';
            document.body.appendChild(errorDiv);
        }
        
        errorDiv.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
        `;
        
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            errorDiv.remove();
        }, 5000);
    }
};

// Enhanced loading state management
const LoadingManager = {
    elements: new Set(),
    
    setLoading: (isLoading, elementId = null) => {
        if (elementId) {
            const element = document.getElementById(elementId);
            if (element) {
                element.classList.toggle('loading', isLoading);
                if (isLoading) {
                    LoadingManager.elements.add(elementId);
                } else {
                    LoadingManager.elements.delete(elementId);
                }
            }
        } else {
            document.querySelector('.loading-overlay').style.display = isLoading ? 'flex' : 'none';
        }
    },
    
    isLoading: () => LoadingManager.elements.size > 0
};

// Enhanced handleMapUpdate function
async function handleMapUpdate(filters) {
    const cacheKey = generateCacheKey(filters);
    
    // Check cache first
    if (statsCache.has(cacheKey)) {
        const cachedData = statsCache.get(cacheKey);
        updateStatistics(cachedData.statistics);
        updatePodThresholdStats(cachedData);
        return;
    }

    try {
        LoadingManager.setLoading(true);
        const response = await fetch('/api/stats', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(filters)
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const data = await response.json();
        
        // Cache the results
        statsCache.set(cacheKey, data);
        manageCacheSize();

        // Update statistics
        updateStatistics(data.statistics);
        updatePodThresholdStats(data);

        // Clear existing layers
        Object.values(layers).forEach(layer => layer.clearLayers());

        // Create a bounds object to track the extent of all features
        let mapBounds = null;

        // Add FHO layer first (bottom)
        mapBounds = createAndAddLayer(data.geometries.fho, 'FHO', null, layers.fho, mapBounds);
        
        // Add FFWs second (middle)
        mapBounds = createAndAddLayer(data.geometries.ffws_hit, 'FFW', true, layers.ffwsHit, mapBounds);
        mapBounds = createAndAddLayer(data.geometries.ffws_miss, 'FFW', false, layers.ffwsMiss, mapBounds);
        
        // Add LSRs last (top)
        mapBounds = createAndAddLayer(data.geometries.lsrs_hit, 'LSR', true, layers.lsrsHit, mapBounds);
        mapBounds = createAndAddLayer(data.geometries.lsrs_miss, 'LSR', false, layers.lsrsMiss, mapBounds);

        // Fit map to bounds
        map.fitBounds(mapBounds?.isValid() ? mapBounds : config.bounds.CONUS);
    } catch (error) {
        ErrorHandler.handleError(error, () => {
            map.fitBounds(config.bounds.CONUS);
            ErrorHandler.showError('Failed to update map. Please try again.');
        });
    } finally {
        LoadingManager.setLoading(false);
    }
}

// Debounced updateMap function
let updateMapTimeout;
async function updateMap() {
    clearTimeout(updateMapTimeout);
    updateMapTimeout = setTimeout(async () => {
        const filters = {
            issuance_date: document.getElementById('issuanceDate').value,
            end_date: document.getElementById('endDate').value,
            issuance: document.getElementById('issuance').value,
            forecast_period: document.getElementById('forecastPeriod').value,
            pod_threshold: parseFloat(document.getElementById('podThreshold').value)
        };

        if (!filters.issuance_date || !filters.issuance || !filters.forecast_period) {
            ErrorHandler.showError('Please select all required fields');
            return;
        }

        if (filters.end_date && new Date(filters.end_date) < new Date(filters.issuance_date)) {
            ErrorHandler.showError('End Date cannot be before the FHO Issuance Date');
            document.getElementById('endDate').value = '';
            return;
        }

        await handleMapUpdate(filters);
    }, 300);
}

// Populate date dropdowns
async function loadDates() {
    try {
        console.log("Starting loadDates function...");
        LoadingManager.setLoading(true);
        
        // Add explicit error handling for fetch
        let response;
        try {
            console.log("Fetching dates from /api/available-dates...");
            response = await fetch('/api/available-dates', {
                method: 'GET',
                headers: {
                    'Accept': 'application/json',
                    'Cache-Control': 'no-cache'
                }
            });
            console.log("Fetch response received:", response);
        } catch (fetchError) {
            console.error("Network error during fetch:", fetchError);
            throw new Error(`Failed to fetch dates: ${fetchError.message}`);
        }

        if (!response.ok) {
            console.error("API response not OK:", response.status, response.statusText);
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        let dates;
        try {
            dates = await response.json();
            console.log("Parsed dates from response:", dates);
        } catch (parseError) {
            console.error("Error parsing JSON response:", parseError);
            throw new Error('Failed to parse dates response');
        }
        
        if (!Array.isArray(dates)) {
            console.error("Dates is not an array:", dates);
            throw new Error('Invalid data format: expected array of dates');
        }

        if (dates.length === 0) {
            console.warn("No dates returned from API");
            throw new Error('No dates available');
        }
        
        const issuanceDateSelect = document.getElementById('issuanceDate');
        const endDateSelect = document.getElementById('endDate');
        
        if (!issuanceDateSelect || !endDateSelect) {
            console.error("Select elements not found:", {
                issuanceDateSelect: !!issuanceDateSelect,
                endDateSelect: !!endDateSelect
            });
            throw new Error('Date select elements not found');
        }
        
        console.log("Clearing and populating select elements...");
        // Keep the default "Select" options
        issuanceDateSelect.innerHTML = '<option value="">Select Date</option>';
        endDateSelect.innerHTML = '<option value="">Select End Date</option>';
        
        // Sort dates in descending order (most recent first)
        dates.sort((a, b) => new Date(b) - new Date(a));
        
        // Add dates to select elements
        dates.forEach(date => {
            try {
                const formattedDate = new Date(date).toISOString().split('T')[0];
                issuanceDateSelect.add(new Option(formattedDate, formattedDate));
                endDateSelect.add(new Option(formattedDate, formattedDate));
            } catch (dateError) {
                console.warn(`Error adding date ${date}:`, dateError);
            }
        });
        
        console.log("Date dropdowns populated successfully with", dates.length, "dates");
        
        // Verify options were added
        console.log("issuanceDate options:", issuanceDateSelect.options.length);
        console.log("endDate options:", endDateSelect.options.length);
        
    } catch (error) {
        console.error("Error in loadDates:", error);
        ErrorHandler.showError(`Failed to load available dates: ${error.message}`);
    } finally {
        LoadingManager.setLoading(false);
    }
}

// Initialize tooltips
function initializeTooltips() {
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
}

// Event listeners
document.addEventListener('DOMContentLoaded', async () => {
    console.log("DOMContentLoaded event fired");
    try {
        // Initialize tooltips
        initializeTooltips();
        console.log("Tooltips initialized");

        // Load dates first
        console.log("Starting initial date load...");
        await loadDates();
        console.log("Initial date load completed");

        // Load high-impact events for Quick Select
        await loadHighImpactEvents();

        // Add event listeners for form controls
        const formControls = {
            'issuanceDate': updateMap,
            'endDate': updateMap,
            'issuance': updateMap,
            'forecastPeriod': updateMap,
            'podThreshold': function() {
                updateMap();
                const threshold = this.value;
                const label = document.getElementById('podThresholdLabel');
                if (label) {
                    label.textContent = `POD ≥ ${threshold}`;
                }
            },
            'quickSelect': function(e) {
                try {
                    if (!e.target.value) return;
                    
                    const event = JSON.parse(e.target.value);
                    console.log('Selected event:', event); // Debug logging
                    
                    // Set the date regardless of event type
                    if (event.date) {
                        document.getElementById('issuanceDate').value = event.date;
                    }
                    
                    // If it's an FHO event (has issuance property)
                    if (event.issuance) {
                        // Set issuance time (AM/PM to 00Z/12Z)
                        const issuanceValue = event.issuance.toLowerCase() === 'am' ? '00Z' : '12Z';
                        document.getElementById('issuance').value = issuanceValue;
                        
                        // Set forecast period if available
                        if (event.period) {
                            document.getElementById('forecastPeriod').value = event.period;
                        }
                        
                        // Hide the no FHO alert if it was previously shown
                        document.getElementById('noFhoAlert').style.display = 'none';
                    } 
                    // If it's an FFW event (has tag property)
                    else if (event.tag) {
                        // Show the no FHO alert
                        document.getElementById('noFhoAlert').style.display = 'block';
                    }
                    
                    // Update the map
                    updateMap();
                } catch (error) {
                    console.error('Error handling Quick Select change:', error);
                    ErrorHandler.showError('Failed to load selected event. Please try again.');
                }
            }
        };

        // Add listeners with verification
        Object.entries(formControls).forEach(([id, handler]) => {
            const element = document.getElementById(id);
            if (element) {
                element.addEventListener('change', handler);
                console.log(`Event listener added for ${id}`);
            } else {
                console.warn(`Element not found for ${id}`);
            }
        });

        // Add reset view button listener
        const resetButton = document.getElementById('resetView');
        if (resetButton) {
            resetButton.addEventListener('click', () => {
                map.fitBounds(config.bounds.CONUS);
                console.log("Map view reset to CONUS bounds");
            });
            console.log("Reset view button listener added");
        } else {
            console.warn("Reset view button not found");
        }
    } catch (error) {
        console.error("Error during initialization:", error);
        ErrorHandler.showError('Failed to initialize application. Please refresh the page.');
    }
});

// Add CSS for loading states
const style = document.createElement('style');
style.textContent = `
    .loading {
        position: relative;
        color: transparent !important;
    }
    .loading::after {
        content: '';
        position: absolute;
        top: 50%;
        left: 50%;
        width: 20px;
        height: 20px;
        margin: -10px 0 0 -10px;
        border: 2px solid #f3f3f3;
        border-top: 2px solid #3498db;
        border-radius: 50%;
        animation: spin 1s linear infinite;
    }
    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
`;
document.head.appendChild(style);

// Load high-impact events for Quick Select
function loadHighImpactEvents() {
    fetch('/api/high-impact-events')
        .then(response => response.json())
        .then(data => {
            const quickSelect = document.getElementById('quickSelect');
            const groups = quickSelect.getElementsByTagName('optgroup');
            
            // Clear existing options
            for (let group of groups) {
                group.innerHTML = '';
            }
            
            // Add Considerable FHO events
            const considerableGroup = groups[0];
            data.considerable_fho.forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${event.date} ${event.issuance} (Days ${event.period})`;
                considerableGroup.appendChild(option);
            });
            
            // Add Catastrophic FHO events
            const catastrophicGroup = groups[1];
            data.catastrophic_fho.forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${event.date} ${event.issuance} (Days ${event.period})`;
                catastrophicGroup.appendChild(option);
            });
            
            // Add High Impact FFWs with no FHO
            const ffwGroup = groups[2];
            data.high_impact_ffws.forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${event.date} ${event.tag} FFW (${event.issued}-${event.expired})`;
                ffwGroup.appendChild(option);
            });
        });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    loadHighImpactEvents();
}); 