// Configuration object for styles and constants
const config = {
    bounds: {
        CONUS: [
            [24.396308, -125.000000],
            [49.384358, -66.934570]
        ]
    },
    colors: {
        hit: '#059669',
        miss: '#dc2626',
        fho: '#1e40af',
        catastrophic: '#b91c1c',
        considerable: '#d97706',
        noTag: '#6b7280'
    },
    styles: {
        fho: {
            color: '#1e40af',
            weight: 2,
            opacity: 0.85,
            fillOpacity: 0.15
        },
        lsrsHit: {
            color: '#059669',
            weight: 2,
            opacity: 0.9,
            fillOpacity: 0.6
        },
        lsrsMiss: {
            color: '#dc2626',
            weight: 2,
            opacity: 0.9,
            fillOpacity: 0.6
        },
        ffwsHit: {
            color: '#059669',
            weight: 2,
            opacity: 0.7,
            fillOpacity: 0.25
        },
        ffwsMiss: {
            color: '#dc2626',
            weight: 2,
            opacity: 0.7,
            fillOpacity: 0.2
        },
        ffwsCatastrophic: {
            color: '#b91c1c',
            weight: 3,
            opacity: 0.9,
            fillOpacity: 0.45
        },
        ffwsConsiderable: {
            color: '#d97706',
            weight: 3,
            opacity: 0.9,
            fillOpacity: 0.45
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

L.control.scale({ imperial: true, metric: true, position: 'bottomleft' }).addTo(map);

// Layer groups (ordered back-to-front for z-stacking)
const layers = {
    fho: L.layerGroup(),
    fhoPolygons: L.layerGroup(),
    ffwsMiss: L.layerGroup(),
    ffwsHit: L.layerGroup(),
    lsrsMiss: L.layerGroup(),
    lsrsHit: L.layerGroup()
};

// Add all layers except fho (merged) which starts unchecked
Object.entries(layers).forEach(([key, layer]) => {
    if (key !== 'fho') layer.addTo(map);
});

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
    return `${filters.issuance_date}_${filters.end_date}_${filters.issuance}_${filters.forecast_period}`;
}

// Helper function to create LSR markers
function createLSRMarker(feature, latlng, isHit) {
    if (isHit) {
        return L.circleMarker(latlng, {
            ...config.pointMarkers,
            fillColor: config.colors.hit,
            color: '#065f46'
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

let podDonutChart = null;
let hitsBarChart = null;
let areaBinChart = null;
let dailyPodChart = null;

function initCharts() {
    const donutCtx = document.getElementById('podDonut');
    const barCtx = document.getElementById('hitsBar');
    if (!donutCtx || !barCtx || typeof Chart === 'undefined') return;

    podDonutChart = new Chart(donutCtx, {
        type: 'doughnut',
        data: {
            labels: ['Hits', 'Misses'],
            datasets: [{
                data: [0, 1],
                backgroundColor: ['#059669', '#dc2626'],
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            cutout: '65%',
            plugins: { legend: { display: false }, tooltip: { enabled: true } }
        }
    });

    hitsBarChart = new Chart(barCtx, {
        type: 'bar',
        data: {
            labels: [''],
            datasets: [
                { label: 'LSR Hits', data: [0], backgroundColor: '#059669' },
                { label: 'FFW Hits', data: [0], backgroundColor: '#10b981' },
                { label: 'LSR Misses', data: [0], backgroundColor: '#dc2626' },
                { label: 'FFW Misses', data: [0], backgroundColor: '#f87171' }
            ]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { stacked: true, display: false },
                y: { stacked: true, display: false }
            },
            plugins: { legend: { display: false }, tooltip: { enabled: true } }
        }
    });

    const abCtx = document.getElementById('areaBinChart');
    if (abCtx) {
        areaBinChart = new Chart(abCtx, {
            type: 'bar',
            data: { labels: [], datasets: [{ data: [], backgroundColor: [], barThickness: 14 }] },
            options: {
                responsive: true, maintainAspectRatio: false, indexAxis: 'y',
                scales: { x: { max: 100, display: false }, y: { ticks: { font: { size: 10 } } } },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => {
                                const item = ctx.chart._areaBinData?.[ctx.dataIndex];
                                return item ? `POD: ${item.pod}% — ${item.verified} of ${item.total} polygons verified` : '';
                            }
                        }
                    }
                }
            }
        });
    }

    const dpCtx = document.getElementById('dailyPodChart');
    if (dpCtx) {
        dailyPodChart = new Chart(dpCtx, {
            type: 'line',
            data: { labels: [], datasets: [{ data: [], borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,0.1)', fill: true, tension: 0.3, pointRadius: 3, pointBackgroundColor: '#2563eb', borderWidth: 2 }] },
            options: {
                responsive: true, maintainAspectRatio: false,
                scales: {
                    x: { ticks: { font: { size: 9 }, maxRotation: 45 } },
                    y: { min: 0, max: 100, ticks: { font: { size: 9 }, callback: v => v + '%' } }
                },
                plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => `POD: ${ctx.parsed.y}%` } } }
            }
        });
    }
}

function updateCharts(stats) {
    if (podDonutChart) {
        podDonutChart.data.datasets[0].data = [stats.total_hits || 0, stats.total_misses || 0];
        podDonutChart.update();
    }
    if (hitsBarChart) {
        hitsBarChart.data.datasets[0].data = [stats.lsr_hits || 0];
        hitsBarChart.data.datasets[1].data = [stats.ffw_hits || 0];
        hitsBarChart.data.datasets[2].data = [stats.lsr_misses || 0];
        hitsBarChart.data.datasets[3].data = [stats.ffw_misses || 0];
        hitsBarChart.update();
    }
}

function animateValue(el, endVal, duration = 400, suffix = '') {
    const startVal = parseFloat(el.textContent) || 0;
    if (startVal === endVal) return;
    const startTime = performance.now();
    const step = (now) => {
        const progress = Math.min((now - startTime) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = startVal + (endVal - startVal) * eased;
        el.textContent = (Number.isInteger(endVal) ? Math.round(current) : current.toFixed(1)) + suffix;
        if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
}

function updateStatistics(stats) {
    const podPct = stats.pod * 100;

    const numericStats = {
        totalHits: stats.total_hits,
        totalMisses: stats.total_misses,
        lsrHits: stats.lsr_hits,
        lsrMisses: stats.lsr_misses,
        ffwHits: stats.ffw_hits,
        ffwMisses: stats.ffw_misses,
        totalDays: stats.total_days
    };

    Object.entries(numericStats).forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (el) {
            el.classList.remove('stat-flash');
            void el.offsetWidth;
            el.classList.add('stat-flash');
            animateValue(el, value);
        }
    });

    const podEl = document.getElementById('pod');
    if (podEl) {
        podEl.classList.remove('stat-flash');
        void podEl.offsetWidth;
        podEl.classList.add('stat-flash');
        animateValue(podEl, podPct, 400, '%');
        podEl.style.color = podPct >= 70 ? 'var(--success-color)' : podPct >= 40 ? 'var(--warning-color)' : 'var(--danger-color)';
    }

    // FAR and CSI
    const farEl = document.getElementById('farValue');
    if (farEl) {
        const farPct = (stats.far || 0) * 100;
        animateValue(farEl, farPct, 400, '%');
        farEl.style.color = farPct <= 30 ? 'var(--success-color)' : farPct <= 60 ? 'var(--warning-color)' : 'var(--danger-color)';
    }
    const csiEl = document.getElementById('csiValue');
    if (csiEl) {
        const csiPct = (stats.csi || 0) * 100;
        animateValue(csiEl, csiPct, 400, '%');
        csiEl.style.color = csiPct >= 40 ? 'var(--success-color)' : csiPct >= 20 ? 'var(--warning-color)' : 'var(--danger-color)';
    }

    const daysEl = document.getElementById('daysIncluded');
    if (daysEl) daysEl.textContent = stats.days_included.join(', ');

    const emptyState = document.getElementById('mapEmptyState');
    if (emptyState) emptyState.style.display = 'none';

    updateCharts(stats);
}

function updateAreaBinChart(data) {
    const section = document.getElementById('areaBinSection');
    const bins = data.area_bin_stats;
    if (!areaBinChart || !bins || !bins.length) {
        if (section) section.style.display = 'none';
        return;
    }
    if (section) section.style.display = '';
    areaBinChart._areaBinData = bins;
    areaBinChart.data.labels = bins.map(b => `${b.bin} km² (${b.verified}/${b.total})`);
    areaBinChart.data.datasets[0].data = bins.map(b => b.pod);
    areaBinChart.data.datasets[0].backgroundColor = bins.map(b =>
        b.pod >= 70 ? '#059669' : b.pod >= 40 ? '#d97706' : '#dc2626'
    );
    areaBinChart.update();

    const infoEl = document.getElementById('areaBinAreaInfo');
    if (infoEl) {
        const totalArea = data.statistics?.total_fho_area_sqkm || 0;
        const verifiedArea = data.statistics?.verified_area_sqkm || 0;
        infoEl.textContent = `${Math.round(verifiedArea).toLocaleString()} / ${Math.round(totalArea).toLocaleString()} km²`;
    }
}

function updateDailyPodChart(data) {
    const section = document.getElementById('dailySeriesSection');
    const series = data.daily_series;
    if (!dailyPodChart || !series || series.length <= 1) {
        if (section) section.style.display = 'none';
        return;
    }
    if (section) section.style.display = '';
    dailyPodChart.data.labels = series.map(d => d.date.slice(5));
    dailyPodChart.data.datasets[0].data = series.map(d => d.pod);
    dailyPodChart.data.datasets[0].pointBackgroundColor = series.map(d =>
        d.pod >= 70 ? '#059669' : d.pod >= 40 ? '#d97706' : '#dc2626'
    );
    dailyPodChart.update();
}

function updateVerifWindow(data) {
    const row = document.getElementById('fhoVerifWindow');
    const val = document.getElementById('fhoVerifWindowValue');
    if (!row || !val) return;
    if (data.verification_window?.start && data.verification_window?.end) {
        const fmt = (iso) => {
            const d = new Date(iso + 'Z');
            return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }) + ' UTC';
        };
        val.textContent = `${fmt(data.verification_window.start)} \u2013 ${fmt(data.verification_window.end)}`;
        row.style.display = '';
    } else {
        row.style.display = 'none';
    }
}

function updatePodThresholdStats(data) {
    if (!data.pod_analysis) return;

    const {
        threshold_percentage = 0,
        above_threshold = 0,
        below_threshold = 0,
        total_polygons = 0
    } = data.pod_analysis;

    const goalPct = parseInt(document.getElementById('podThreshold')?.value || '70');

    const progressBar = document.getElementById('podThresholdProgress');
    if (progressBar) {
        progressBar.style.width = `${threshold_percentage}%`;
        progressBar.setAttribute('aria-valuenow', threshold_percentage);
        progressBar.className = 'progress-bar';
        if (threshold_percentage >= goalPct) progressBar.classList.add('bg-success');
        else if (threshold_percentage >= goalPct * 0.6) progressBar.classList.add('bg-warning');
        else progressBar.classList.add('bg-danger');
    }

    const percentEl = document.getElementById('podThresholdPercent');
    if (percentEl) {
        percentEl.textContent = `${threshold_percentage.toFixed(1)}%`;
        percentEl.className = 'badge';
        if (threshold_percentage >= goalPct) percentEl.classList.add('bg-success');
        else if (threshold_percentage >= goalPct * 0.6) percentEl.classList.add('bg-warning');
        else percentEl.classList.add('bg-danger');
    }

    const countEl = document.getElementById('podThresholdCount');
    if (countEl) countEl.textContent = above_threshold;

    const totalEl = document.getElementById('podTotalCount');
    if (totalEl) totalEl.textContent = total_polygons;
}

// Helper function to create popup content
function formatDateStr(val) {
    if (!val) return 'Unknown';
    try {
        const d = new Date(val);
        if (isNaN(d)) return String(val);
        return d.toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch { return String(val); }
}

function createPopupContent(type, feature, isHit = null) {
    const hitColor = config.colors.hit;
    const missColor = config.colors.miss;

    switch (type) {
        case 'FHO': {
            const issuanceTime = document.getElementById('issuance').value === '00Z' ? 'AM' : 'PM';
            const forecastPeriod = document.getElementById('forecastPeriod').value;
            const issuanceDate = document.getElementById('issuanceDate').value;
            const impactLevel = feature.properties?.impact_level || 'Limited_merged';
            return `
                <div class="warning-popup">
                    <div class="title" style="border-left: 4px solid ${config.colors.fho}; padding-left: 8px;">FHO Forecast Area</div>
                    <div class="details">
                        <div><span class="label">Date:</span> <span class="value">${issuanceDate}</span></div>
                        <div><span class="label">Time:</span> <span class="value">${issuanceTime}</span></div>
                        <div><span class="label">Period:</span> <span class="value">Days ${forecastPeriod}</span></div>
                        <div><span class="label">Impact:</span> <span class="value">${impactLevel}</span></div>
                    </div>
                </div>`;
        }
        case 'LSR': {
            const statusColor = isHit ? hitColor : missColor;
            const statusLabel = isHit ? 'HIT' : 'MISS';
            const eventType = feature.properties.EVENT || 'Unknown';
            return `
                <div class="warning-popup">
                    <div class="title" style="border-left: 4px solid ${statusColor}; padding-left: 8px;">
                        LSR Details <span style="color:${statusColor}; font-size:12px;">[${statusLabel}]</span>
                    </div>
                    <div class="details">
                        <div><span class="label">Event:</span> <span class="value">${eventType}</span></div>
                        <div><span class="label">Time:</span> <span class="value">${formatDateStr(feature.properties.VALID)}</span></div>
                        <div><span class="label">Location:</span> <span class="value">${feature.properties.CITY || ''}, ${feature.properties.STATE || ''}</span></div>
                        <div><span class="label">Source:</span> <span class="value">${feature.properties.SOURCE || 'Unknown'}</span></div>
                    </div>
                </div>`;
        }
        case 'FFW': {
            const statusColor = isHit ? hitColor : missColor;
            const statusLabel = isHit ? 'HIT' : 'MISS';
            return `
                <div class="warning-popup">
                    <div class="title" style="border-left: 4px solid ${statusColor}; padding-left: 8px;">
                        Flash Flood Warning <span style="color:${statusColor}; font-size:12px;">[${statusLabel}]</span>
                    </div>
                    <div class="details">
                        <div><span class="label">Issued:</span> <span class="value">${formatDateStr(feature.properties.ISSUED)}</span></div>
                        <div><span class="label">Expired:</span> <span class="value">${formatDateStr(feature.properties.EXPIRED)}</span></div>
                        <div><span class="label">Impact:</span> <span class="value">${feature.properties.DAMAGTAG || 'No Tag'}</span></div>
                    </div>
                </div>`;
        }
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
        const existing = document.getElementById('error-notification');
        if (existing) existing.remove();

        const toast = document.createElement('div');
        toast.id = 'error-notification';
        toast.className = 'error-toast';
        toast.innerHTML = `
            <svg class="error-toast-icon" viewBox="0 0 20 20" fill="currentColor">
                <path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.168 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 6a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 6zm0 9a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd" />
            </svg>
            <span class="error-toast-message">${message}</span>
            <button type="button" class="btn-close" aria-label="Close"></button>
        `;
        document.body.appendChild(toast);

        toast.querySelector('.btn-close').addEventListener('click', () => {
            toast.classList.add('dismissing');
            toast.addEventListener('animationend', () => toast.remove());
        });

        setTimeout(() => {
            if (toast.parentNode) {
                toast.classList.add('dismissing');
                toast.addEventListener('animationend', () => toast.remove());
            }
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

function resetStatsDisplay() {
    ['pod', 'totalHits', 'totalMisses', 'lsrHits', 'lsrMisses', 'ffwHits', 'ffwMisses', 'totalDays', 'daysIncluded', 'farValue', 'csiValue'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '--';
    });
    const progressBar = document.getElementById('podThresholdProgress');
    if (progressBar) {
        progressBar.style.width = '0%';
        progressBar.setAttribute('aria-valuenow', 0);
    }
    ['podThresholdPercent', 'podThresholdCount', 'podTotalCount'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '--';
    });
    const abSec = document.getElementById('areaBinSection');
    if (abSec) abSec.style.display = 'none';
    const dsSec = document.getElementById('dailySeriesSection');
    if (dsSec) dsSec.style.display = 'none';
}

function countFeatures(geojson) {
    if (!geojson) return 0;
    if (geojson.type === 'FeatureCollection') return geojson.features?.length || 0;
    if (geojson.type === 'Feature') return 1;
    return 0;
}

function updateLayerCounts(data) {
    const counts = {
        lsrsHit: countFeatures(data.geometries.lsrs_hit),
        lsrsMiss: countFeatures(data.geometries.lsrs_miss),
        ffwsHit: countFeatures(data.geometries.ffws_hit),
        ffwsMiss: countFeatures(data.geometries.ffws_miss)
    };
    Object.entries(counts).forEach(([key, count]) => {
        const el = document.getElementById(`count-${key}`);
        if (el) el.textContent = count > 0 ? `(${count})` : '';
    });
}

function renderMapLayers(data) {
    const emptyState = document.getElementById('mapEmptyState');
    Object.values(layers).forEach(layer => layer.clearLayers());

    const fhoEmpty = !data.geometries?.fho?.geometry;
    const lsrEmpty = !data.geometries?.lsrs_hit?.features?.length && !data.geometries?.lsrs_miss?.features?.length;
    const ffwEmpty = !data.geometries?.ffws_hit?.features?.length && !data.geometries?.ffws_miss?.features?.length;
    if (fhoEmpty && lsrEmpty && ffwEmpty && emptyState) {
        const p = emptyState.querySelector('p');
        if (p) p.textContent = 'No FHO polygons found for this selection';
        emptyState.style.display = '';
        return;
    }
    if (emptyState) emptyState.style.display = 'none';

    let mapBounds = null;
    mapBounds = createAndAddLayer(data.geometries.fho, 'FHO', null, layers.fho, mapBounds);

    // Per-polygon layer: individual FHO polygons colored by verification status
    if (data.geometries?.fho_polygons?.features?.length) {
        const polys = data.geometries.fho_polygons.features;
        const verifiedCount = polys.filter(f => f.properties.verified).length;

        const polyLayer = L.geoJSON(data.geometries.fho_polygons, {
            style: (feature) => {
                const v = feature.properties.verified;
                const hc = feature.properties.hit_count;
                return {
                    color: v ? (hc >= 3 ? '#047857' : '#059669') : '#dc2626',
                    weight: v ? 2.5 : 2,
                    opacity: 0.85,
                    fillOpacity: v ? 0.20 : 0.12,
                    fillColor: v ? '#059669' : '#dc2626',
                    dashArray: v ? null : '6, 4'
                };
            },
            onEachFeature: (feature, layer) => {
                const p = feature.properties;
                const statusColor = p.verified ? config.colors.hit : config.colors.miss;
                const statusLabel = p.verified ? 'VERIFIED' : 'UNVERIFIED';
                const eventBar = p.hit_count > 0
                    ? `<div style="margin-top:4px;display:flex;gap:4px;align-items:center;">
                        <div style="background:${config.colors.hit};height:6px;border-radius:3px;flex:${p.lsr_hits}" title="${p.lsr_hits} LSRs"></div>
                        <div style="background:#10b981;height:6px;border-radius:3px;flex:${p.ffw_hits}" title="${p.ffw_hits} FFWs"></div>
                       </div>
                       <div style="font-size:10px;color:#6b7280;margin-top:2px;">${p.lsr_hits} LSR + ${p.ffw_hits} FFW = ${p.hit_count} events</div>`
                    : '<div style="font-size:10px;color:#dc2626;margin-top:4px;">No events intersected this polygon</div>';

                layer.bindPopup(`
                    <div class="warning-popup">
                        <div class="title" style="border-left: 4px solid ${statusColor}; padding-left: 8px;">
                            FHO Polygon <span style="color:${statusColor}; font-size:11px; font-weight:600;">[${statusLabel}]</span>
                        </div>
                        <div class="details">
                            <div><span class="label">Date:</span> <span class="value">${p.issuance_date} ${p.issuance_time}</span></div>
                            <div><span class="label">Period:</span> <span class="value">Days ${p.forecast_period}</span></div>
                            <div><span class="label">Area:</span> <span class="value">${p.area_sqkm.toLocaleString()} km² (${p.area_bin})</span></div>
                            ${eventBar}
                        </div>
                    </div>`, { maxWidth: 280 });
            }
        });
        polyLayer.addTo(layers.fhoPolygons);
        if (polyLayer.getBounds().isValid()) {
            mapBounds = mapBounds ? mapBounds.extend(polyLayer.getBounds()) : polyLayer.getBounds();
        }

        // Update per-polygon count badge in legend
        const countEl = document.getElementById('count-fhoPolygons');
        if (countEl) countEl.textContent = `(${verifiedCount}/${polys.length} verified)`;
    } else {
        const countEl = document.getElementById('count-fhoPolygons');
        if (countEl) countEl.textContent = '';
    }

    mapBounds = createAndAddLayer(data.geometries.ffws_hit, 'FFW', true, layers.ffwsHit, mapBounds);
    mapBounds = createAndAddLayer(data.geometries.ffws_miss, 'FFW', false, layers.ffwsMiss, mapBounds);
    mapBounds = createAndAddLayer(data.geometries.lsrs_hit, 'LSR', true, layers.lsrsHit, mapBounds);
    mapBounds = createAndAddLayer(data.geometries.lsrs_miss, 'LSR', false, layers.lsrsMiss, mapBounds);

    // Z-order: FHO merged (back) → FHO polygons → FFW miss → FFW hit → LSR miss → LSR hit (front)
    [layers.fho, layers.fhoPolygons, layers.ffwsMiss, layers.ffwsHit, layers.lsrsMiss, layers.lsrsHit].forEach(lg => {
        if (map.hasLayer(lg)) {
            lg.eachLayer(l => { if (typeof l.bringToFront === 'function') l.bringToFront(); });
        }
    });

    map.fitBounds(mapBounds?.isValid() ? mapBounds : config.bounds.CONUS);

    updateLayerCounts(data);
}

let currentAbortController = null;

async function handleMapUpdate(filters) {
    const cacheKey = generateCacheKey(filters);

    if (statsCache.has(cacheKey)) {
        const cachedData = statsCache.get(cacheKey);
        updateStatistics(cachedData.statistics);
        updatePodThresholdStats(cachedData);
        updateVerifWindow(cachedData);
        updateAreaBinChart(cachedData);
        updateDailyPodChart(cachedData);
        renderMapLayers(cachedData);
        return;
    }

    if (currentAbortController) {
        currentAbortController.abort();
    }
    currentAbortController = new AbortController();

    try {
        LoadingManager.setLoading(true);
        const response = await fetch('/api/stats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(filters),
            signal: currentAbortController.signal
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        statsCache.set(cacheKey, data);
        manageCacheSize();

        updateStatistics(data.statistics);
        updatePodThresholdStats(data);
        updateVerifWindow(data);
        updateAreaBinChart(data);
        updateDailyPodChart(data);
        renderMapLayers(data);
        pushState();
    } catch (error) {
        if (error.name === 'AbortError') return;
        resetStatsDisplay();
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
            pod_threshold: parseInt(document.getElementById('podThreshold').value) / 100
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

async function loadDates() {
    try {
        LoadingManager.setLoading(true);

        const response = await fetch('/api/available-dates', {
            method: 'GET',
            headers: { 'Accept': 'application/json', 'Cache-Control': 'no-cache' }
        });

        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

        const dates = await response.json();
        if (!Array.isArray(dates) || dates.length === 0) throw new Error('No dates available');

        const issuanceDateSelect = document.getElementById('issuanceDate');
        const endDateSelect = document.getElementById('endDate');
        if (!issuanceDateSelect || !endDateSelect) throw new Error('Date select elements not found');

        issuanceDateSelect.innerHTML = '<option value="">Select Date</option>';
        endDateSelect.innerHTML = '<option value="">Select End Date</option>';

        dates.sort((a, b) => new Date(b) - new Date(a));

        allDates = dates.map(d => new Date(d).toISOString().split('T')[0]);

        allDates.forEach(formattedDate => {
            issuanceDateSelect.add(new Option(formattedDate, formattedDate));
            endDateSelect.add(new Option(formattedDate, formattedDate));
        });

        buildYearPills(allDates);
    } catch (error) {
        console.error('Error in loadDates:', error);
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

// Segmented button group helper
function initSegGroup(groupId, hiddenId, onChange) {
    const group = document.getElementById(groupId);
    const hidden = document.getElementById(hiddenId);
    if (!group || !hidden) return;
    group.querySelectorAll('.seg-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            group.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            hidden.value = btn.dataset.value;
            if (onChange) onChange();
        });
    });
}

function setSegValue(groupId, hiddenId, value) {
    const group = document.getElementById(groupId);
    const hidden = document.getElementById(hiddenId);
    if (!group || !hidden) return;
    hidden.value = value;
    group.querySelectorAll('.seg-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.value === value);
    });
}

// All loaded dates (kept for year-pill filtering and prev/next stepping)
let allDates = [];
let activeYearFilter = null;

function buildYearPills(dates) {
    const container = document.getElementById('yearPills');
    if (!container) return;
    const years = [...new Set(dates.map(d => d.substring(0, 4)))].sort();
    container.innerHTML = '';
    const allBtn = document.createElement('button');
    allBtn.type = 'button';
    allBtn.className = 'year-pill active';
    allBtn.textContent = 'All';
    allBtn.addEventListener('click', () => {
        activeYearFilter = null;
        container.querySelectorAll('.year-pill').forEach(p => p.classList.remove('active'));
        allBtn.classList.add('active');
        filterDatesByYear(null);
    });
    container.appendChild(allBtn);

    years.forEach(y => {
        const pill = document.createElement('button');
        pill.type = 'button';
        pill.className = 'year-pill';
        pill.textContent = y;
        pill.addEventListener('click', () => {
            activeYearFilter = y;
            container.querySelectorAll('.year-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            filterDatesByYear(y);
        });
        container.appendChild(pill);
    });
}

function filterDatesByYear(year) {
    const sel = document.getElementById('issuanceDate');
    if (!sel) return;
    const currentVal = sel.value;
    sel.innerHTML = '<option value="">Select Date</option>';
    const filtered = year ? allDates.filter(d => d.startsWith(year)) : allDates;
    filtered.forEach(d => sel.add(new Option(d, d)));
    if (filtered.includes(currentVal)) sel.value = currentVal;

    const endSel = document.getElementById('endDate');
    if (endSel) {
        const endVal = endSel.value;
        endSel.innerHTML = '<option value="">Select End Date</option>';
        filtered.forEach(d => endSel.add(new Option(d, d)));
        if (filtered.includes(endVal)) endSel.value = endVal;
    }
    annotateDateDropdown();
}

function stepDate(direction) {
    const sel = document.getElementById('issuanceDate');
    if (!sel) return;
    const options = Array.from(sel.options).filter(o => o.value).map(o => o.value);
    if (!options.length) return;
    const idx = options.indexOf(sel.value);
    const newIdx = idx + direction;
    if (newIdx >= 0 && newIdx < options.length) {
        sel.value = options[newIdx];
        updateMap();
    }
}

function pushState() {
    const params = new URLSearchParams();
    const date = document.getElementById('issuanceDate')?.value;
    const iss = document.getElementById('issuance')?.value;
    const fp = document.getElementById('forecastPeriod')?.value;
    const end = document.getElementById('endDate')?.value;
    const pod = document.getElementById('podThreshold')?.value;
    if (date) params.set('date', date);
    if (iss) params.set('iss', iss);
    if (fp) params.set('fp', fp);
    if (end) params.set('end', end);
    if (pod && pod !== '70') params.set('pod', pod);
    const hash = params.toString();
    if (hash !== location.hash.slice(1)) history.replaceState(null, '', '#' + hash);
}

function restoreState() {
    if (!location.hash || location.hash.length < 2) return false;
    const params = new URLSearchParams(location.hash.slice(1));
    const date = params.get('date');
    const iss = params.get('iss');
    const fp = params.get('fp');
    const end = params.get('end');
    const pod = params.get('pod');
    let restored = false;
    if (date) { document.getElementById('issuanceDate').value = date; restored = true; }
    if (iss) setSegValue('issuanceGroup', 'issuance', iss);
    if (fp) setSegValue('forecastPeriodGroup', 'forecastPeriod', fp);
    if (end) document.getElementById('endDate').value = end;
    if (pod) {
        document.getElementById('podThreshold').value = pod;
        const sliderLabel = document.getElementById('podSliderLabel');
        if (sliderLabel) sliderLabel.textContent = `${pod}%`;
        const statsLabel = document.getElementById('podThresholdLabel');
        if (statsLabel) statsLabel.textContent = `POD \u2265 ${(pod / 100).toFixed(2)}`;
    }
    return restored;
}

// Event listeners
document.addEventListener('DOMContentLoaded', async () => {
    try {
        initializeTooltips();
        initCharts();
        await loadDates();
        await loadHighImpactEvents();

        initSegGroup('issuanceGroup', 'issuance', updateMap);
        initSegGroup('forecastPeriodGroup', 'forecastPeriod', updateMap);

        const restored = restoreState();
        if (!restored && allDates.length > 0) {
            document.getElementById('issuanceDate').value = allDates[0];
        }
        updateMap();

        document.getElementById('datePrev')?.addEventListener('click', () => stepDate(1));
        document.getElementById('dateNext')?.addEventListener('click', () => stepDate(-1));

        const formControls = {
            'issuanceDate': updateMap,
            'endDate': updateMap,
            'podThreshold': function() {
                const sliderLabel = document.getElementById('podSliderLabel');
                if (sliderLabel) sliderLabel.textContent = `${this.value}%`;
                const statsLabel = document.getElementById('podThresholdLabel');
                if (statsLabel) statsLabel.textContent = `POD \u2265 ${(this.value / 100).toFixed(2)}`;
                // Re-render POD threshold stats with cached data (no refetch needed)
                const cacheKey = generateCacheKey({
                    issuance_date: document.getElementById('issuanceDate').value,
                    end_date: document.getElementById('endDate').value,
                    issuance: document.getElementById('issuance').value,
                    forecast_period: document.getElementById('forecastPeriod').value
                });
                if (statsCache.has(cacheKey)) updatePodThresholdStats(statsCache.get(cacheKey));
            },
            'quickSelect': function(e) {
                try {
                    if (!e.target.value) return;

                    const event = JSON.parse(e.target.value);

                    if (event.date) {
                        document.getElementById('issuanceDate').value = event.date;
                    }

                    if (event.issuance) {
                        const issuanceValue = event.issuance.toLowerCase() === 'am' ? '00Z' : '12Z';
                        setSegValue('issuanceGroup', 'issuance', issuanceValue);

                        if (event.period) {
                            setSegValue('forecastPeriodGroup', 'forecastPeriod', event.period);
                        }

                        const noFhoAlert = document.getElementById('noFhoAlert');
                        if (noFhoAlert) noFhoAlert.style.display = 'none';
                    } else if (event.tag) {
                        const noFhoAlert = document.getElementById('noFhoAlert');
                        if (noFhoAlert) noFhoAlert.style.display = 'block';
                    }

                    updateMap();
                } catch (error) {
                    console.error('Error handling Quick Select change:', error);
                    ErrorHandler.showError('Failed to load selected event. Please try again.');
                }
            }
        };

        Object.entries(formControls).forEach(([id, handler]) => {
            const element = document.getElementById(id);
            if (element) {
                element.addEventListener('change', handler);
            }
        });

        const podSlider = document.getElementById('podThreshold');
        if (podSlider) {
            podSlider.addEventListener('input', function() {
                const label = document.getElementById('podSliderLabel');
                if (label) label.textContent = `${this.value}%`;
            });
        }

        const resetButton = document.getElementById('resetView');
        if (resetButton) {
            resetButton.addEventListener('click', () => {
                map.fitBounds(config.bounds.CONUS);
            });
        }

        document.querySelectorAll('.legend-toggle input[data-layer]').forEach(cb => {
            cb.addEventListener('change', function() {
                const layerGroup = layers[this.dataset.layer];
                if (!layerGroup) return;
                if (this.checked) {
                    map.addLayer(layerGroup);
                } else {
                    map.removeLayer(layerGroup);
                }
            });
        });

        initThemeToggle();

        document.getElementById('exportPng')?.addEventListener('click', async () => {
            if (typeof html2canvas === 'undefined') { ErrorHandler.showError('PNG export not available'); return; }
            const btn = document.getElementById('exportPng');
            const origHtml = btn.innerHTML;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm" style="width:12px;height:12px;"></span>';
            btn.disabled = true;
            try {
                const target = document.querySelector('.container-fluid');
                const canvas = await html2canvas(target, {
                    useCORS: true,
                    allowTaint: true,
                    scale: 2,
                    backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--card-background').trim() || '#ffffff',
                    ignoreElements: (el) => el.classList?.contains('loading-overlay')
                });
                const link = document.createElement('a');
                const date = document.getElementById('issuanceDate')?.value || 'export';
                const iss = document.getElementById('issuance')?.value || '';
                const fp = document.getElementById('forecastPeriod')?.value || '';
                link.download = `FHO_${date}_${iss}_Days${fp}.png`;
                link.href = canvas.toDataURL('image/png');
                link.click();
            } catch (e) {
                console.error('PNG export error:', e);
                ErrorHandler.showError('Failed to capture screenshot');
            } finally {
                btn.innerHTML = origHtml;
                btn.disabled = false;
            }
        });

        document.getElementById('exportCsv')?.addEventListener('click', async () => {
            const filters = {
                issuance_date: document.getElementById('issuanceDate').value,
                end_date: document.getElementById('endDate').value,
                issuance: document.getElementById('issuance').value,
                forecast_period: document.getElementById('forecastPeriod').value
            };
            if (!filters.issuance_date) { ErrorHandler.showError('Select a date first'); return; }
            try {
                const resp = await fetch('/api/export-csv', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(filters)
                });
                if (!resp.ok) throw new Error('Export failed');
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `fho_verification_${filters.issuance_date}.csv`;
                a.click();
                URL.revokeObjectURL(url);
            } catch (e) {
                ErrorHandler.showError('Failed to export CSV');
            }
        });

        document.getElementById('copyLink')?.addEventListener('click', () => {
            pushState();
            navigator.clipboard.writeText(window.location.href).then(() => {
                const btn = document.getElementById('copyLink');
                const orig = btn.innerHTML;
                btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" clip-rule="evenodd"/></svg> Copied!';
                setTimeout(() => { btn.innerHTML = orig; }, 2000);
            }).catch(() => ErrorHandler.showError('Failed to copy link'));
        });

        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
            switch (e.key) {
                case 'ArrowLeft': e.preventDefault(); stepDate(1); break;
                case 'ArrowRight': e.preventDefault(); stepDate(-1); break;
                case '1': setSegValue('forecastPeriodGroup', 'forecastPeriod', '1-3'); updateMap(); break;
                case '2': setSegValue('forecastPeriodGroup', 'forecastPeriod', '4-7'); updateMap(); break;
                case '3': setSegValue('forecastPeriodGroup', 'forecastPeriod', '1-7'); updateMap(); break;
                case 'a': case 'A': setSegValue('issuanceGroup', 'issuance', '00Z'); updateMap(); break;
                case 'p': case 'P': setSegValue('issuanceGroup', 'issuance', '12Z'); updateMap(); break;
            }
        });
    } catch (error) {
        console.error('Error during initialization:', error);
        ErrorHandler.showError('Failed to initialize application. Please refresh the page.');
    }
});


const darkBaseMap = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 20,
    minZoom: 0
});

const sunIcon = '<path stroke-linecap="round" stroke-linejoin="round" d="M12 3v2.25m6.364.386l-1.591 1.591M21 12h-2.25m-.386 6.364l-1.591-1.591M12 18.75V21m-4.773-4.227l-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z" />';
const moonIcon = '<path stroke-linecap="round" stroke-linejoin="round" d="M21.752 15.002A9.718 9.718 0 0112.478 3.34a9.72 9.72 0 109.274 11.662z" />';

function initThemeToggle() {
    const saved = localStorage.getItem('fho-theme');
    if (saved === 'dark') applyTheme('dark');

    const btn = document.getElementById('themeToggle');
    if (btn) {
        btn.addEventListener('click', () => {
            const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
            applyTheme(next);
            localStorage.setItem('fho-theme', next);
        });
    }
}

function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const icon = document.getElementById('themeIcon');
    if (icon) icon.innerHTML = theme === 'dark' ? sunIcon : moonIcon;

    if (theme === 'dark') {
        map.removeLayer(lightBaseMap);
        darkBaseMap.addTo(map);
    } else {
        map.removeLayer(darkBaseMap);
        lightBaseMap.addTo(map);
    }
}

const dateEventMap = {};

function loadHighImpactEvents() {
    return fetch('/api/high-impact-events')
        .then(response => {
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            return response.json();
        })
        .then(data => {
            const quickSelect = document.getElementById('quickSelect');
            if (!quickSelect) return;
            const groups = quickSelect.getElementsByTagName('optgroup');

            for (let group of groups) {
                group.innerHTML = '';
            }

            (data.considerable_fho || []).forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${event.date} ${event.issuance} (Days ${event.period})`;
                groups[0].appendChild(option);
                if (!dateEventMap[event.date]) dateEventMap[event.date] = new Set();
                dateEventMap[event.date].add('con');
            });

            (data.catastrophic_fho || []).forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${event.date} ${event.issuance} (Days ${event.period})`;
                groups[1].appendChild(option);
                if (!dateEventMap[event.date]) dateEventMap[event.date] = new Set();
                dateEventMap[event.date].add('cat');
            });

            (data.high_impact_ffws || []).forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${event.date} ${event.tag} FFW (${event.issued}-${event.expired})`;
                groups[2].appendChild(option);
                if (!dateEventMap[event.date]) dateEventMap[event.date] = new Set();
                dateEventMap[event.date].add('ffw');
            });

            annotateDateDropdown();
        })
        .catch(error => {
            console.error('Failed to load high-impact events:', error);
            ErrorHandler.showError('Failed to load Quick Select events.');
        });
}

function annotateDateDropdown() {
    const sel = document.getElementById('issuanceDate');
    if (!sel) return;
    for (const opt of sel.options) {
        const tags = dateEventMap[opt.value];
        if (!tags || tags.size === 0) continue;
        const indicators = [];
        if (tags.has('cat')) indicators.push('\u25CF');
        else if (tags.has('con')) indicators.push('\u25CB');
        if (tags.has('ffw')) indicators.push('\u26A0');
        if (indicators.length) opt.textContent = `${opt.value}  ${indicators.join(' ')}`;
    }
} 