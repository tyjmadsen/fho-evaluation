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
            labels: ['Captured', 'Missed'],
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
                { label: 'LSR Inside', data: [0], backgroundColor: '#059669' },
                { label: 'FFW Inside', data: [0], backgroundColor: '#10b981' },
                { label: 'LSR Outside', data: [0], backgroundColor: '#dc2626' },
                { label: 'FFW Outside', data: [0], backgroundColor: '#f87171' }
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
                                return item ? `${item.pod}% — ${item.verified} of ${item.total} polygons verified` : '';
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
                plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => `Verified: ${ctx.parsed.y}%` } } }
            }
        });
    }
}

function updateCharts(stats, evidence) {
    if (podDonutChart) {
        podDonutChart.data.labels = ['Captured', 'Missed'];
        podDonutChart.data.datasets[0].data = [evidence.total_inside || 0, evidence.total_outside || 0];
        podDonutChart.update();
    }
    if (hitsBarChart) {
        hitsBarChart.data.datasets[0].data = [evidence.lsr_inside || 0];
        hitsBarChart.data.datasets[1].data = [evidence.ffw_inside || 0];
        hitsBarChart.data.datasets[2].data = [evidence.lsr_outside || 0];
        hitsBarChart.data.datasets[3].data = [evidence.ffw_outside || 0];
        hitsBarChart.update();
    }
}

// animateValue() is provided by shared.js

function updateStatistics(stats, evidence) {
    // Headline: Event Capture Rate
    const ratePct = (stats.event_capture_rate || 0) * 100;
    const totalEvents = stats.total_events || 0;
    const totalHits = evidence.total_inside || 0;

    const goalPct = parseInt(document.getElementById('podThreshold')?.value, 10) || 70;

    const rateEl = document.getElementById('captureRate');
    if (rateEl) {
        rateEl.classList.remove('stat-flash');
        void rateEl.offsetWidth;
        rateEl.classList.add('stat-flash');
        animateValue(rateEl, ratePct, 400, '%');
        rateEl.style.color = colorForVerificationRate(ratePct, goalPct);
    }

    const subtitleEl = document.getElementById('captureRateSubtitle');
    if (subtitleEl) {
        subtitleEl.textContent = totalEvents > 0
            ? `${totalHits} of ${totalEvents} flood events captured by FHO`
            : 'No flood events in verification window';
    }

    // Polygon verification details (collapsible section)
    const verifiedEl = document.getElementById('verifiedPolygons');
    const unverifiedEl = document.getElementById('unverifiedPolygons');
    const totalPolyEl = document.getElementById('totalPolygons');
    if (verifiedEl) animateValue(verifiedEl, stats.verified_polygons || 0);
    if (unverifiedEl) animateValue(unverifiedEl, stats.unverified_polygons || 0);
    if (totalPolyEl) totalPolyEl.textContent = stats.total_polygons || 0;

    const verifPct = stats.total_polygons > 0
        ? (stats.verified_polygons / stats.total_polygons * 100)
        : 0;
    const progressBar = document.getElementById('podThresholdProgress');
    if (progressBar) {
        progressBar.style.width = `${verifPct}%`;
        progressBar.setAttribute('aria-valuenow', verifPct);
        progressBar.className = progressBarClassesForRate(verifPct, goalPct);
    }
    const percentEl = document.getElementById('podThresholdPercent');
    if (percentEl) {
        percentEl.textContent = `${verifPct.toFixed(1)}%`;
        percentEl.className = badgeClassesForRate(verifPct, goalPct);
    }

    // Evidence counts
    const evidenceStats = {
        lsrInside: evidence.lsr_inside,
        lsrOutside: evidence.lsr_outside,
        ffwInside: evidence.ffw_inside,
        ffwOutside: evidence.ffw_outside,
        totalDays: stats.total_days
    };
    Object.entries(evidenceStats).forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (el) {
            el.classList.remove('stat-flash');
            void el.offsetWidth;
            el.classList.add('stat-flash');
            animateValue(el, value);
        }
    });

    const daysEl = document.getElementById('daysIncluded');
    if (daysEl) daysEl.textContent = (stats.days_included || []).join(', ');

    const emptyState = document.getElementById('mapEmptyState');
    if (emptyState) {
        emptyState.style.display = 'none';
        emptyState.setAttribute('aria-hidden', 'true');
    }

    updateCharts(stats, evidence);
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
    const validSeries = (series || []).filter(d => d && typeof d.date === 'string');
    if (!dailyPodChart || validSeries.length <= 1) {
        if (section) section.style.display = 'none';
        return;
    }
    if (section) section.style.display = '';
    dailyPodChart.data.labels = validSeries.map(d => d.date.slice(5));
    dailyPodChart.data.datasets[0].data = validSeries.map(d => d.verification_rate);
    dailyPodChart.data.datasets[0].pointBackgroundColor = validSeries.map(d =>
        d.verification_rate >= 70 ? '#059669' : d.verification_rate >= 40 ? '#d97706' : '#dc2626'
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

// Polygon verification stats are now integrated into updateStatistics()

// formatDateStr() is provided by shared.js

function createPopupContent(type, feature, isHit = null) {
    const hitColor = config.colors.hit;
    const missColor = config.colors.miss;

    switch (type) {
        case 'FHO': {
            const issuanceTime = (document.getElementById('issuance')?.value || 'AM').toUpperCase();
            const forecastPeriod = document.getElementById('forecastPeriod')?.value || '';
            const issuanceDate = document.getElementById('issuanceDate')?.value || '';
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
            const eventType = escapeHtml(feature.properties.EVENT || 'Unknown');
            return `
                <div class="warning-popup">
                    <div class="title" style="border-left: 4px solid ${statusColor}; padding-left: 8px;">
                        LSR Details <span style="color:${statusColor}; font-size:12px;">[${statusLabel}]</span>
                    </div>
                    <div class="details">
                        <div><span class="label">Event:</span> <span class="value">${eventType}</span></div>
                        <div><span class="label">Time:</span> <span class="value">${formatDateStr(feature.properties.VALID)}</span></div>
                        <div><span class="label">Location:</span> <span class="value">${escapeHtml(feature.properties.CITY || '')}, ${escapeHtml(feature.properties.STATE || '')}</span></div>
                        <div><span class="label">Source:</span> <span class="value">${escapeHtml(feature.properties.SOURCE || 'Unknown')}</span></div>
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
                        <div><span class="label">Impact:</span> <span class="value">${escapeHtml(feature.properties.DAMAGTAG || 'No Tag')}</span></div>
                    </div>
                </div>`;
        }
        default:
            return '';
    }
}

// Helper function to get style for FFW based on damage tag
function getFFWStyle(feature, isHit) {
    const tag = feature.properties?.DAMAGTAG;
    if (tag === 'CATASTROPHIC') {
        return config.styles.ffwsCatastrophic;
    } else if (tag === 'CONSIDERABLE') {
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
    showError: (message) => showError(message)  // delegates to shared.js
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
            const overlay = document.querySelector('.loading-overlay');
            if (overlay) overlay.style.display = isLoading ? 'flex' : 'none';
        }
    },
    
    isLoading: () => LoadingManager.elements.size > 0
};

function resetStatsDisplay() {
    ['captureRate', 'verifiedPolygons', 'unverifiedPolygons',
     'totalPolygons', 'lsrInside', 'lsrOutside', 'ffwInside',
     'ffwOutside', 'totalDays', 'daysIncluded'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '--';
    });
    const subtitleEl = document.getElementById('captureRateSubtitle');
    if (subtitleEl) subtitleEl.textContent = 'Select a date to view capture rate';
    const progressBar = document.getElementById('podThresholdProgress');
    if (progressBar) {
        progressBar.style.width = '0%';
        progressBar.setAttribute('aria-valuenow', 0);
        progressBar.className = 'progress-bar';
    }
    const percentEl = document.getElementById('podThresholdPercent');
    if (percentEl) {
        percentEl.textContent = '-';
        percentEl.className = 'badge bg-primary';
    }
    if (podDonutChart) {
        podDonutChart.data.datasets[0].data = [0, 1];
        podDonutChart.update();
    }
    if (hitsBarChart) {
        hitsBarChart.data.datasets.forEach(ds => { ds.data = [0]; });
        hitsBarChart.update();
    }
    if (areaBinChart) {
        areaBinChart.data.labels = [];
        areaBinChart.data.datasets[0].data = [];
        areaBinChart.update();
    }
    if (dailyPodChart) {
        dailyPodChart.data.labels = [];
        dailyPodChart.data.datasets[0].data = [];
        dailyPodChart.update();
    }
    const abSec = document.getElementById('areaBinSection');
    if (abSec) abSec.style.display = 'none';
    const dsSec = document.getElementById('dailySeriesSection');
    if (dsSec) dsSec.style.display = 'none';
    const vwRow = document.getElementById('fhoVerifWindow');
    if (vwRow) vwRow.style.display = 'none';
}

function countFeatures(geojson) {
    if (!geojson) return 0;
    if (geojson.type === 'FeatureCollection') return geojson.features?.length || 0;
    if (geojson.type === 'Feature') return 1;
    return 0;
}

function updateLayerCounts(data) {
    const geom = data?.geometries || {};
    const counts = {
        lsrsHit: countFeatures(geom.lsrs_hit),
        lsrsMiss: countFeatures(geom.lsrs_miss),
        ffwsHit: countFeatures(geom.ffws_hit),
        ffwsMiss: countFeatures(geom.ffws_miss)
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
        emptyState.setAttribute('aria-hidden', 'false');
        return;
    }
    if (emptyState) {
        emptyState.style.display = 'none';
        emptyState.setAttribute('aria-hidden', 'true');
    }

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
                            <div><span class="label">Area:</span> <span class="value">${(p.area_sqkm ?? 0).toLocaleString()} km² (${p.area_bin || 'N/A'})</span></div>
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
let _fetchGeneration = 0;

async function handleMapUpdate(filters) {
    const cacheKey = generateCacheKey(filters);

    if (statsCache.has(cacheKey)) {
        const cachedData = statsCache.get(cacheKey);
        updateStatistics(cachedData.statistics, cachedData.evidence || {});
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
    const gen = ++_fetchGeneration;

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

        if (gen !== _fetchGeneration) return;

        statsCache.set(cacheKey, data);
        manageCacheSize();

        updateStatistics(data.statistics, data.evidence || {});
        updateVerifWindow(data);
        updateAreaBinChart(data);
        updateDailyPodChart(data);
        renderMapLayers(data);
        pushState();
    } catch (error) {
        if (error.name === 'AbortError') return;
        if (gen !== _fetchGeneration) return;
        resetStatsDisplay();
        ErrorHandler.handleError(error, () => {
            map.fitBounds(config.bounds.CONUS);
            ErrorHandler.showError('Failed to update map. Please try again.');
        });
    } finally {
        if (gen === _fetchGeneration) {
            LoadingManager.setLoading(false);
        }
    }
}

// Debounced updateMap function
let updateMapTimeout;
async function updateMap() {
    clearTimeout(updateMapTimeout);
    updateMapTimeout = setTimeout(async () => {
        const issuanceDateEl = document.getElementById('issuanceDate');
        const endDateEl = document.getElementById('endDate');
        const issuanceEl = document.getElementById('issuance');
        const forecastPeriodEl = document.getElementById('forecastPeriod');
        const podThresholdEl = document.getElementById('podThreshold');
        if (!issuanceDateEl || !issuanceEl || !forecastPeriodEl) return;

        const filters = {
            issuance_date: issuanceDateEl.value,
            end_date: endDateEl ? endDateEl.value : '',
            issuance: issuanceEl.value,
            forecast_period: forecastPeriodEl.value,
            pod_threshold: podThresholdEl ? parseInt(podThresholdEl.value) / 100 : 0.7
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

        allDates = dates.map(d => {
            try { return new Date(d).toISOString().split('T')[0]; }
            catch { return null; }
        }).filter(Boolean);

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

// initSegGroup() and setSegValue() are provided by shared.js

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
    const dateEl = document.getElementById('issuanceDate');
    if (date && dateEl) { dateEl.value = date; restored = true; }
    if (iss) setSegValue('issuanceGroup', 'issuance', iss);
    if (fp) setSegValue('forecastPeriodGroup', 'forecastPeriod', fp);
    const endEl = document.getElementById('endDate');
    if (end && endEl) endEl.value = end;
    const podEl = document.getElementById('podThreshold');
    if (pod && podEl) podEl.value = pod;
    return restored;
}

const darkBaseMap = createDarkBaseMap();
const dateEventMap = {};

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
            'podThreshold': function() {},
            'quickSelect': function(e) {
                try {
                    if (!e.target.value) return;

                    const event = JSON.parse(e.target.value);

                    if (event.date) {
                        document.getElementById('issuanceDate').value = event.date;
                    }

                    if (event.issuance) {
                        setSegValue('issuanceGroup', 'issuance', event.issuance.toUpperCase());

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

        // podThreshold is a hidden input; no slider listener needed

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

        initThemeToggle(map, lightBaseMap, darkBaseMap);

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
                issuance_date: document.getElementById('issuanceDate')?.value,
                end_date: document.getElementById('endDate')?.value,
                issuance: document.getElementById('issuance')?.value,
                forecast_period: document.getElementById('forecastPeriod')?.value
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
                case 'a': case 'A': setSegValue('issuanceGroup', 'issuance', 'AM'); updateMap(); break;
                case 'p': case 'P': setSegValue('issuanceGroup', 'issuance', 'PM'); updateMap(); break;
            }
        });
    } catch (error) {
        console.error('Error during initialization:', error);
        ErrorHandler.showError('Failed to initialize application. Please refresh the page.');
    }
});

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