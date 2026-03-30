// showError(), animateValue(), formatDateStr(), initSegGroup(), setSegValue(),
// initThemeToggle(), createDarkBaseMap() are provided by shared.js

const CONUS_CENTER = [39.8283, -98.5795];
const CONUS_ZOOM = 4;

const map = L.map('map').setView(CONUS_CENTER, CONUS_ZOOM);

map.on('click', (e) => {
    const hits = getFeaturesAtPoint(e.latlng);
    if (hits.length) openStackedPopup(e.latlng);
});

// initSegGroup() and setSegValue() are provided by shared.js

{
    const _ilLabel = document.getElementById('impactLevelLabel');
    const _ilHidden = document.getElementById('impactLevel');
    if (_ilLabel && _ilHidden) _ilLabel.textContent = _ilHidden.value;
}

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

L.control.layers({ "Light": lightBaseMap, "Satellite": satelliteLayer }, null, {
    position: 'bottomright'
}).addTo(map);

map.zoomControl.setPosition('bottomright');

L.control.scale({ imperial: true, metric: true, position: 'bottomleft' }).addTo(map);

const styles = {
    fhoConsiderable: {
        color: '#92400e', weight: 2, opacity: 0.6, fillOpacity: 0.08, fillColor: '#fbbf24', dashArray: '8, 6'
    },
    fhoCatastrophic: {
        color: '#7f1d1d', weight: 2, opacity: 0.6, fillOpacity: 0.08, fillColor: '#fca5a5', dashArray: '8, 6'
    },
    limited: {
        color: '#1e40af', weight: 1, opacity: 0.5, fillOpacity: 0.03, fillColor: '#1e40af', dashArray: '5, 5'
    },
    catastrophicHit: {
        color: '#b91c1c', weight: 2, opacity: 1, fillOpacity: 0.4, fillColor: '#b91c1c'
    },
    catastrophicMiss: {
        color: '#dc2626', weight: 2, opacity: 0.8, fillOpacity: 0, fillColor: 'transparent', dashArray: '5, 5'
    },
    considerableHit: {
        color: '#d97706', weight: 2, opacity: 1, fillOpacity: 0.4, fillColor: '#d97706'
    },
    considerableMiss: {
        color: '#f59e0b', weight: 2, opacity: 0.8, fillOpacity: 0, fillColor: 'transparent', dashArray: '5, 5'
    },
    noTag: {
        color: '#6b7280', weight: 1, opacity: 0.7, fillOpacity: 0.1, fillColor: '#6b7280'
    }
};

let activeLayers = [];
const layerOrder = ['limited', 'fho', 'noTag', 'considerable', 'catastrophic', 'lsr'];

function lsrRadius(zoom) {
    if (zoom <= 4) return 3;
    if (zoom <= 6) return 4;
    if (zoom <= 8) return 6;
    if (zoom <= 10) return 8;
    return 10;
}

map.on('zoomend', () => {
    const r = lsrRadius(map.getZoom());
    activeLayers.forEach(l => {
        if (!l._isLsrLayer) return;
        l.eachLayer(marker => {
            if (marker instanceof L.CircleMarker) marker.setRadius(r);
        });
    });
});

// animateValue() is provided by shared.js

function resetIbwStats() {
    ['podValue', 'hitsValue', 'missesValue', 'noTagValue', 'totalFfwsValue', 'lsrsInWindowValue'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '--';
    });
    const vwRow = document.getElementById('verifWindowRow');
    if (vwRow) vwRow.style.display = 'none';
}

let _legendBuilt = false;
const _legendZGroupMap = {
    limited: 'limited',
    fho: 'fho',
    noTag: 'noTag',
    considerableLayers: 'considerable',
    catastrophicLayers: 'catastrophic',
    lsrs: 'lsr'
};

function updateLegend() {
    const legend = document.getElementById('mapLegend');
    if (!legend) return;
    const body = legend.querySelector('.legend-body');
    if (!body) return;

    if (!_legendBuilt) {
        body.innerHTML = `
            <label class="legend-toggle">
                <input type="checkbox" checked data-layer="catastrophicLayers">
                <span class="legend-color" style="background-color: ${styles.catastrophicHit.color};"></span>
                <span>Cat. Hit</span>
            </label>
            <label class="legend-toggle legend-sub">
                <span class="legend-color" style="background-color: ${styles.catastrophicMiss.color}; opacity: 0.6;"></span>
                <span>Cat. Miss</span>
            </label>
            <label class="legend-toggle">
                <input type="checkbox" checked data-layer="considerableLayers">
                <span class="legend-color" style="background-color: ${styles.considerableHit.color};"></span>
                <span>Con. Hit</span>
            </label>
            <label class="legend-toggle legend-sub">
                <span class="legend-color" style="background-color: ${styles.considerableMiss.color}; opacity: 0.6;"></span>
                <span>Con. Miss</span>
            </label>
            <label class="legend-toggle">
                <input type="checkbox" checked data-layer="noTag">
                <span class="legend-color" style="background-color: ${styles.noTag.color};"></span>
                <span>No Tag</span>
            </label>
            <label class="legend-toggle">
                <input type="checkbox" checked data-layer="fho">
                <span class="legend-color" style="background-color: ${styles.fhoConsiderable.fillColor}; border: 2px dashed ${styles.fhoConsiderable.color};"></span>
                <span>FHO Con.</span>
            </label>
            <label class="legend-toggle legend-sub">
                <span class="legend-color" style="background-color: ${styles.fhoCatastrophic.fillColor}; border: 2px dashed ${styles.fhoCatastrophic.color};"></span>
                <span>FHO Cat.</span>
            </label>
            <label class="legend-toggle">
                <input type="checkbox" checked data-layer="limited">
                <span class="legend-color" style="background-color: ${styles.limited.color}; opacity: 0.5;"></span>
                <span>FHO Limited</span>
            </label>
            <label class="legend-toggle">
                <input type="checkbox" checked data-layer="lsrs">
                <span class="legend-color" style="background-color: #7c3aed; border-radius: 50%;"></span>
                <span>LSRs</span>
            </label>
        `;

        body.querySelectorAll('input[data-layer]').forEach(cb => {
            cb.addEventListener('change', function() {
                const zGroup = _legendZGroupMap[this.dataset.layer];
                if (!zGroup) return;
                activeLayers.filter(l => l._zGroup === zGroup).forEach(l => {
                    if (this.checked) { map.addLayer(l); } else { map.removeLayer(l); }
                });
            });
        });
        _legendBuilt = true;
    }
}

function _syncLegendVisibility() {
    const legend = document.getElementById('mapLegend');
    if (!legend) return;
    legend.querySelectorAll('input[data-layer]').forEach(cb => {
        const zGroup = _legendZGroupMap[cb.dataset.layer];
        if (!zGroup) return;
        activeLayers.filter(l => l._zGroup === zGroup).forEach(l => {
            if (cb.checked) { map.addLayer(l); } else { map.removeLayer(l); }
        });
    });
}

document.addEventListener('DOMContentLoaded', () => {
    fetch('/api/available-dates')
        .then(response => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        })
        .then(dates => {
            const datePicker = document.getElementById('issuanceDate');
            const restored = ibwRestoreState();
            if (!restored && dates.length > 0) {
                datePicker.value = dates[dates.length - 1];
            }
            updateMap();
        })
        .catch(error => {
            console.error('Failed to load available dates:', error);
            showError('Failed to load available dates.');
        });

    fetch('/api/high-impact-events')
        .then(response => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        })
        .then(data => {
            const quickSelect = document.getElementById('quickSelect');
            if (!quickSelect) return;
            const groups = quickSelect.getElementsByTagName('optgroup');

            for (let group of groups) {
                group.innerHTML = '';
            }

            const conEvents = data.considerable_fho || [];
            const catEvents = data.catastrophic_fho || [];
            const ffwEvents = data.high_impact_ffws || [];

            const fmtDate = (d) => { const p = d.split('-'); return `${p[1]}/${p[2]}/${p[0].slice(2)}`; };

            conEvents.forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${fmtDate(event.date)} ${event.issuance} D${event.period}`;
                groups[0].appendChild(option);
            });

            catEvents.forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${fmtDate(event.date)} ${event.issuance} D${event.period}`;
                groups[1].appendChild(option);
            });

            ffwEvents.forEach(event => {
                const option = document.createElement('option');
                option.value = JSON.stringify(event);
                option.textContent = `${fmtDate(event.date)} ${event.tag}`;
                groups[2].appendChild(option);
            });

            groups[0].label = `Considerable (${conEvents.length})`;
            groups[1].label = `Catastrophic (${catEvents.length})`;
            groups[2].label = `FFWs — No FHO (${ffwEvents.length})`;

            updateEventStepButtons();
        })
        .catch(error => {
            console.error('Failed to load high-impact events:', error);
            showError('Failed to load Quick Select events.');
        });
});

document.getElementById('issuanceDate')?.addEventListener('change', () => {
    const qs = document.getElementById('quickSelect');
    if (qs) qs.selectedIndex = 0;
    updateEventStepButtons();
    updateMap();
});

initSegGroup('issuanceGroup', 'issuance', updateMap);
initSegGroup('forecastPeriodGroup', 'forecastPeriod', updateMap);
initSegGroup('impactLevelGroup', 'impactLevel', function() {
    const lbl = document.getElementById('impactLevelLabel');
    const val = document.getElementById('impactLevel');
    if (lbl && val) lbl.textContent = val.value;
    updateMap();
});

document.getElementById('quickSelect')?.addEventListener('change', function(e) {
    if (!e.target.value) return;

    try {
        const event = JSON.parse(e.target.value);

        if (event.date) {
            document.getElementById('issuanceDate').value = event.date;
        }

        if (event.issuance) {
            setSegValue('issuanceGroup', 'issuance', event.issuance.toUpperCase());

            if (event.period) {
                setSegValue('forecastPeriodGroup', 'forecastPeriod', event.period);
            }

            const ilLabel = document.getElementById('impactLevelLabel');
            const selectedGroup = e.target.selectedOptions[0]?.parentElement?.label || '';
            if (selectedGroup.includes('Considerable')) {
                setSegValue('impactLevelGroup', 'impactLevel', 'Considerable');
                if (ilLabel) ilLabel.textContent = 'Considerable';
            } else if (selectedGroup.includes('Catastrophic')) {
                setSegValue('impactLevelGroup', 'impactLevel', 'Catastrophic');
                if (ilLabel) ilLabel.textContent = 'Catastrophic';
            }

            const noFhoAlert = document.getElementById('noFhoAlert');
            if (noFhoAlert) noFhoAlert.style.display = 'none';
        }
        else if (event.tag) {
            setSegValue('issuanceGroup', 'issuance', 'AM');
            setSegValue('forecastPeriodGroup', 'forecastPeriod', '1-3');
            setSegValue('impactLevelGroup', 'impactLevel', 'Considerable');
            const ilLabel = document.getElementById('impactLevelLabel');
            if (ilLabel) ilLabel.textContent = 'Considerable';

            const noFhoAlert = document.getElementById('noFhoAlert');
            if (noFhoAlert) noFhoAlert.style.display = 'block';
        }

        updateMap();
        updateEventStepButtons();
    } catch (error) {
        console.error('Error handling Quick Select change:', error);
        showError('Failed to load selected event.');
        resetIbwStats();
    }
});

function stepEvent(direction) {
    const qs = document.getElementById('quickSelect');
    if (!qs) return;
    const options = Array.from(qs.options).filter(o => o.value);
    if (!options.length) return;
    const currentIdx = options.findIndex(o => o.selected);
    let nextIdx;
    if (currentIdx < 0) {
        nextIdx = direction > 0 ? 0 : options.length - 1;
    } else {
        nextIdx = currentIdx + direction;
    }
    if (nextIdx < 0 || nextIdx >= options.length) return;
    qs.value = options[nextIdx].value;
    qs.dispatchEvent(new Event('change'));
}

function updateEventStepButtons() {
    const qs = document.getElementById('quickSelect');
    const prevBtn = document.getElementById('eventPrev');
    const nextBtn = document.getElementById('eventNext');
    const countEl = document.getElementById('eventPosition');
    if (!qs || !prevBtn || !nextBtn) return;
    const options = Array.from(qs.options).filter(o => o.value);
    const currentIdx = options.findIndex(o => o.selected);
    prevBtn.disabled = currentIdx <= 0;
    nextBtn.disabled = currentIdx < 0 || currentIdx >= options.length - 1;
    if (countEl) {
        countEl.textContent = currentIdx >= 0 ? `${currentIdx + 1} / ${options.length}` : `${options.length} events`;
    }
}

document.getElementById('eventPrev')?.addEventListener('click', () => stepEvent(-1));
document.getElementById('eventNext')?.addEventListener('click', () => stepEvent(1));

// updateEventStepButtons is called inside the quickSelect change handler above

function safeAddGeoJSON(geojsonData, zGroup, style, layerType) {
    if (!geojsonData) return null;

    let geoJSONLayer = null;

    const wireClick = (feature, lyr) => {
        const content = buildPopupContent(feature);
        if (!content) return;

        lyr._popupContent = content;
        lyr._featureType = feature.properties?.type || '';
        lyr._geojsonFeature = feature;
        const tagSubs = (parent) => {
            if (typeof parent.eachLayer === 'function') {
                parent.eachLayer(sub => {
                    sub._popupContent = content;
                    sub._featureType = feature.properties?.type || '';
                    sub._geojsonFeature = feature;
                    sub._parentLayer = lyr;
                    tagSubs(sub);
                });
            }
        };
        tagSubs(lyr);
    };

    if (geojsonData.type === 'Feature') {
        geojsonData = { type: 'FeatureCollection', features: [geojsonData] };
    }

    if (geojsonData.type === 'FeatureCollection' && geojsonData.features && geojsonData.features.length > 0) {
        geoJSONLayer = L.geoJSON(geojsonData, {
            style: feature => {
                let base;
                if (feature.properties && feature.properties.DAMAGTAG) {
                    if (feature.properties.DAMAGTAG === 'CATASTROPHIC') {
                        base = layerType === 'Hit' ? styles.catastrophicHit : styles.catastrophicMiss;
                    } else if (feature.properties.DAMAGTAG === 'CONSIDERABLE') {
                        base = layerType === 'Hit' ? styles.considerableHit : styles.considerableMiss;
                    }
                }
                if (!base && feature.properties && feature.properties.type === 'Limited') {
                    base = styles.limited;
                }
                return base || style;
            },
            onEachFeature: (feature, lyr) => {
                if (!feature.properties) feature.properties = {};
                if (!feature.properties.type) {
                    feature.properties.type = layerType;
                }
                wireClick(feature, lyr);
            }
        }).addTo(map);
        geoJSONLayer._zGroup = zGroup;
        activeLayers.push(geoJSONLayer);
    }

    return geoJSONLayer;
}

// Client-side response cache (mirrors app.js pattern)
const ibwStatsCache = new Map();
const IBW_MAX_CACHE_SIZE = 30;

function ibwManageCacheSize() {
    if (ibwStatsCache.size > IBW_MAX_CACHE_SIZE) {
        const oldestKey = ibwStatsCache.keys().next().value;
        ibwStatsCache.delete(oldestKey);
    }
}

function ibwCacheKey(filters) {
    return `${filters.issuance_date}_${filters.issuance}_${filters.forecast_period}_${filters.impact_level}`;
}

let _lastIbwRenderedDate = null;

function _renderIbwData(data, { fitView = false } = {}) {
    const emptyState = document.getElementById('mapEmptyState');
    const hasGeometries = data.geometries && (
        data.geometries.limited?.features?.length ||
        data.geometries.fho_considerable?.geometry ||
        data.geometries.fho_catastrophic?.geometry ||
        data.geometries.no_tag?.features?.length ||
        data.geometries.misses?.features?.length ||
        data.geometries.hits?.features?.length ||
        data.geometries.lsrs?.features?.length
    );
    if (emptyState) {
        emptyState.style.display = hasGeometries ? 'none' : '';
        emptyState.setAttribute('aria-hidden', hasGeometries ? 'true' : 'false');
    }

    if (data.statistics) {
        const podEl = document.getElementById('podValue');
        if (podEl) {
            podEl.classList.remove('stat-flash');
            void podEl.offsetWidth;
            podEl.classList.add('stat-flash');
            const ratePct = (data.statistics.capture_rate || 0) * 100;
            animateValue(podEl, ratePct, 400, '%');
            podEl.style.color = colorForVerificationRate(ratePct, 70);
        }

        const intStats = {
            hitsValue: data.statistics.hits,
            missesValue: data.statistics.misses,
            noTagValue: data.statistics.ffws_no_tag,
            totalFfwsValue: data.statistics.total_ffws,
            lsrsInWindowValue: data.statistics.total_lsrs
        };
        Object.entries(intStats).forEach(([id, val]) => {
            const el = document.getElementById(id);
            if (el) {
                el.classList.remove('stat-flash');
                void el.offsetWidth;
                el.classList.add('stat-flash');
                animateValue(el, val != null ? val : 0);
            }
        });
    }

    const vwRow = document.getElementById('verifWindowRow');
    const vwVal = document.getElementById('verifWindowValue');
    if (vwRow && vwVal) {
        if (data.verification_window?.start && data.verification_window?.end) {
            const fmt = (iso) => {
                const d = new Date(iso + 'Z');
                return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }) + ' UTC';
            };
            vwVal.textContent = `${fmt(data.verification_window.start)} \u2013 ${fmt(data.verification_window.end)}`;
            vwRow.style.display = '';
        } else {
            vwRow.style.display = 'none';
        }
    }

    if (data.geometries) {
        let bounds = null;

        safeAddGeoJSON(data.geometries.limited, 'limited', styles.limited, 'Limited');
        safeAddGeoJSON(data.geometries.fho_considerable, 'fho', styles.fhoConsiderable, 'Considerable');
        safeAddGeoJSON(data.geometries.fho_catastrophic, 'fho', styles.fhoCatastrophic, 'Catastrophic');
        safeAddGeoJSON(data.geometries.no_tag, 'noTag', styles.noTag, 'NoTag');

        if (data.geometries.misses?.features) {
            const considerableMisses = {
                type: 'FeatureCollection',
                features: data.geometries.misses.features.filter(f => f.properties?.DAMAGTAG === 'CONSIDERABLE')
            };
            safeAddGeoJSON(considerableMisses, 'considerable', styles.considerableMiss, 'Miss');
            const catastrophicMisses = {
                type: 'FeatureCollection',
                features: data.geometries.misses.features.filter(f => f.properties?.DAMAGTAG === 'CATASTROPHIC')
            };
            safeAddGeoJSON(catastrophicMisses, 'catastrophic', styles.catastrophicMiss, 'Miss');
        }

        if (data.geometries.hits?.features) {
            const considerableHits = {
                type: 'FeatureCollection',
                features: data.geometries.hits.features.filter(f => f.properties?.DAMAGTAG === 'CONSIDERABLE')
            };
            safeAddGeoJSON(considerableHits, 'considerable', styles.considerableHit, 'Hit');
            const catastrophicHits = {
                type: 'FeatureCollection',
                features: data.geometries.hits.features.filter(f => f.properties?.DAMAGTAG === 'CATASTROPHIC')
            };
            safeAddGeoJSON(catastrophicHits, 'catastrophic', styles.catastrophicHit, 'Hit');
        }

        if (data.geometries.other_impact?.features) {
            const considerableOther = {
                type: 'FeatureCollection',
                features: data.geometries.other_impact.features.filter(f => f.properties?.DAMAGTAG === 'CONSIDERABLE')
            };
            safeAddGeoJSON(considerableOther, 'considerable', styles.considerableHit, 'OtherImpact');
            const catastrophicOther = {
                type: 'FeatureCollection',
                features: data.geometries.other_impact.features.filter(f => f.properties?.DAMAGTAG === 'CATASTROPHIC')
            };
            safeAddGeoJSON(catastrophicOther, 'catastrophic', styles.catastrophicHit, 'OtherImpact');
        }

        if (data.geometries.lsrs?.features?.length) {
            const lsrLayer = L.geoJSON(data.geometries.lsrs, {
                pointToLayer: (feature, latlng) => {
                    return L.circleMarker(latlng, {
                        radius: lsrRadius(map.getZoom()),
                        fillColor: '#7c3aed',
                        color: '#4c1d95',
                        weight: 1.5,
                        opacity: 1,
                        fillOpacity: 0.8
                    });
                },
                onEachFeature: (feature, lyr) => {
                    if (!feature.properties) feature.properties = {};
                    feature.properties.type = 'LSR';
                    const content = buildPopupContent(feature);
                    if (content) {
                        lyr._popupContent = content;
                        lyr._featureType = 'LSR';
                        lyr._geojsonFeature = feature;
                    }
                }
            }).addTo(map);
            lsrLayer._zGroup = 'lsr';
            lsrLayer._isLsrLayer = true;
            activeLayers.push(lsrLayer);
        }

        layerOrder.forEach(zGroup => {
            const matching = activeLayers.filter(l => l._zGroup === zGroup);
            matching.forEach(l => l.bringToFront());
        });

        updateLegend();
        _syncLegendVisibility();

        activeLayers.forEach(lyr => {
            if (lyr && typeof lyr.getBounds === 'function') {
                const layerBounds = lyr.getBounds();
                if (layerBounds && layerBounds.isValid()) {
                    bounds = bounds ? bounds.extend(layerBounds) : layerBounds;
                }
            }
        });

        if (fitView) {
            if (bounds && bounds.isValid()) {
                map.fitBounds(bounds);
            } else {
                map.setView(CONUS_CENTER, CONUS_ZOOM);
            }
        }
    } else if (fitView) {
        map.setView(CONUS_CENTER, CONUS_ZOOM);
    }
    ibwPushState();
}

let ibwUpdateTimeout;
let ibwAbortController = null;
let _ibwFetchGen = 0;

function updateMap() {
    clearTimeout(ibwUpdateTimeout);
    ibwUpdateTimeout = setTimeout(() => {
        if (ibwAbortController) ibwAbortController.abort();
        ibwAbortController = new AbortController();
        _doUpdateMap(ibwAbortController.signal);
    }, 250);
}

function _doUpdateMap(signal) {
    const loadingOverlay = document.querySelector('.loading-overlay');

    const dateEl = document.getElementById('issuanceDate');
    const issEl = document.getElementById('issuance');
    const fpEl = document.getElementById('forecastPeriod');
    const ilEl = document.getElementById('impactLevel');
    if (!dateEl || !issEl || !fpEl || !ilEl) return;

    const filters = {
        issuance_date: dateEl.value,
        issuance: issEl.value,
        forecast_period: fpEl.value,
        impact_level: ilEl.value
    };

    if (!filters.issuance_date || !filters.issuance || !filters.forecast_period) {
        return;
    }

    const dateKey = filters.issuance_date;
    const dateChanged = dateKey !== _lastIbwRenderedDate;
    _lastIbwRenderedDate = dateKey;
    const renderOpts = { fitView: dateChanged };

    const cacheKey = ibwCacheKey(filters);
    if (ibwStatsCache.has(cacheKey)) {
        activeLayers.forEach(lyr => { if (lyr) map.removeLayer(lyr); });
        activeLayers = [];
        _renderIbwData(ibwStatsCache.get(cacheKey), renderOpts);
        return;
    }

    activeLayers.forEach(lyr => { if (lyr) map.removeLayer(lyr); });
    activeLayers = [];

    const gen = ++_ibwFetchGen;
    if (loadingOverlay) loadingOverlay.style.display = 'flex';

    fetch('/api/ibw-stats', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(filters),
        signal
    })
    .then(response => {
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        return response.json();
    })
    .then(data => {
        if (gen !== _ibwFetchGen) return;
        if (data.error) {
            throw new Error(data.error);
        }

        ibwStatsCache.set(cacheKey, data);
        ibwManageCacheSize();
        _renderIbwData(data, renderOpts);
    })
    .catch(error => {
        if (error.name === 'AbortError') return;
        if (gen !== _ibwFetchGen) return;
        console.error('Error:', error);
        showError('Failed to load verification data. Please try again.');
        resetIbwStats();
        map.setView(CONUS_CENTER, CONUS_ZOOM);
    })
    .finally(() => {
        if (gen === _ibwFetchGen && loadingOverlay) {
            loadingOverlay.style.display = 'none';
        }
    });
}

// formatDateStr() is provided by shared.js

const darkBaseMap = createDarkBaseMap();
initThemeToggle(map, lightBaseMap, darkBaseMap);

const legendToggle = document.getElementById('legendToggle');
if (legendToggle) {
    legendToggle.addEventListener('click', () => {
        const legend = document.getElementById('mapLegend');
        if (!legend) return;
        legend.classList.toggle('collapsed');
        const btn = legendToggle.querySelector('.legend-collapse-btn');
        if (btn) btn.innerHTML = legend.classList.contains('collapsed') ? '&#9654;' : '&#9660;';
    });
}

function ibwPushState() {
    const params = new URLSearchParams();
    const date = document.getElementById('issuanceDate')?.value;
    const iss = document.getElementById('issuance')?.value;
    const fp = document.getElementById('forecastPeriod')?.value;
    const il = document.getElementById('impactLevel')?.value;
    if (date) params.set('date', date);
    if (iss) params.set('iss', iss);
    if (fp) params.set('fp', fp);
    if (il) params.set('il', il);
    const hash = params.toString();
    if (hash !== location.hash.slice(1)) history.replaceState(null, '', '#' + hash);
}

function ibwRestoreState() {
    if (!location.hash || location.hash.length < 2) return false;
    const params = new URLSearchParams(location.hash.slice(1));
    const date = params.get('date');
    const iss = params.get('iss');
    const fp = params.get('fp');
    const il = params.get('il');
    let restored = false;
    const dateEl = document.getElementById('issuanceDate');
    if (date && dateEl) { dateEl.value = date; restored = true; }
    if (iss) setSegValue('issuanceGroup', 'issuance', iss);
    if (fp) setSegValue('forecastPeriodGroup', 'forecastPeriod', fp);
    if (il) {
        setSegValue('impactLevelGroup', 'impactLevel', il);
        const labelEl = document.getElementById('impactLevelLabel');
        if (labelEl) labelEl.textContent = il;
    }
    return restored;
}

document.getElementById('ibwExportPng')?.addEventListener('click', async () => {
    if (typeof html2canvas === 'undefined') { showError('PNG export not available'); return; }
    const btn = document.getElementById('ibwExportPng');
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
        const il = document.getElementById('impactLevel')?.value || '';
        link.download = `IBW_${date}_${il}.png`;
        link.href = canvas.toDataURL('image/png');
        link.click();
    } catch (e) {
        console.error('PNG export error:', e);
        showError('Failed to capture screenshot');
    } finally {
        btn.innerHTML = origHtml;
        btn.disabled = false;
    }
});

document.getElementById('ibwCopyLink')?.addEventListener('click', () => {
    ibwPushState();
    navigator.clipboard.writeText(window.location.href).then(() => {
        const btn = document.getElementById('ibwCopyLink');
        const orig = btn.innerHTML;
        btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" clip-rule="evenodd"/></svg> Copied!';
        setTimeout(() => { btn.innerHTML = orig; }, 2000);
    }).catch(() => showError('Failed to copy link'));
});

function buildPopupContent(feature) {
    if (!feature.properties) return '';

    const hitColor = '#059669';
    const missColor = '#dc2626';

    if (feature.properties.type === 'Considerable' ||
        feature.properties.type === 'Catastrophic' ||
        feature.properties.type === 'Limited') {
        const typeColors = { Catastrophic: '#b91c1c', Considerable: '#d97706', Limited: '#1e40af' };
        const borderColor = typeColors[feature.properties.type] || '#1e40af';
        const popupDate = document.getElementById('issuanceDate')?.value || 'Unknown';
        const popupIss = document.getElementById('issuance')?.value || 'Unknown';
        const popupFp = document.getElementById('forecastPeriod')?.value || 'Unknown';
        return `
            <div class="warning-popup">
                <div class="title" style="border-left: 4px solid ${borderColor}; padding-left: 8px;">FHO Forecast Area</div>
                <div class="details">
                    <div><span class="label">Impact:</span> <span class="value">${feature.properties.type}</span></div>
                    <div><span class="label">Date:</span> <span class="value">${popupDate}</span></div>
                    <div><span class="label">Time:</span> <span class="value">${popupIss}</span></div>
                    <div><span class="label">Period:</span> <span class="value">Days ${popupFp}</span></div>
                </div>
            </div>`;
    } else if (feature.properties.type === 'LSR') {
        const event = escapeHtml(feature.properties.EVENT || feature.properties.TYPETEXT || 'Unknown');
        const remarks = escapeHtml(feature.properties.REMARKS || feature.properties.REMARK || 'None');
        return `
            <div class="warning-popup">
                <div class="title" style="border-left: 4px solid #7c3aed; padding-left: 8px;">Local Storm Report</div>
                <div class="details">
                    <div><span class="label">Event:</span> <span class="value">${event}</span></div>
                    <div><span class="label">Location:</span> <span class="value">${escapeHtml(feature.properties.CITY || 'Unknown')}, ${escapeHtml(feature.properties.STATE || '')}</span></div>
                    <div><span class="label">Time:</span> <span class="value">${formatDateStr(feature.properties.VALID)}</span></div>
                    <div><span class="label">Source:</span> <span class="value">${escapeHtml(feature.properties.SOURCE || 'Unknown')}</span></div>
                    <div><span class="label">Remarks:</span> <span class="value">${remarks}</span></div>
                </div>
            </div>`;
    } else if (feature.properties.popup_content || feature.properties.DAMAGTAG !== undefined) {
        if (feature.properties.popup_content) {
            return feature.properties.popup_content;
        }
        const isHit = feature.properties.type === 'Hit' || feature.properties.type === 'OtherImpact';
        const statusColor = isHit ? hitColor : missColor;
        const statusLabel = isHit ? 'HIT' : 'MISS';
        return `
            <div class="warning-popup">
                <div class="title" style="border-left: 4px solid ${statusColor}; padding-left: 8px;">
                    Flash Flood Warning <span style="color:${statusColor}; font-size:12px;">[${statusLabel}]</span>
                </div>
                <div class="details">
                    <div><span class="label">Impact:</span> <span class="value">${escapeHtml(feature.properties.DAMAGTAG || 'No Tag')}</span></div>
                    <div><span class="label">Issued:</span> <span class="value">${formatDateStr(feature.properties.ISSUED)}</span></div>
                    <div><span class="label">Expired:</span> <span class="value">${formatDateStr(feature.properties.EXPIRED)}</span></div>
                </div>
            </div>`;
    }
    return '';
}

function collectLeafLayers(parent, results) {
    if (typeof parent.eachLayer === 'function') {
        parent.eachLayer(child => collectLeafLayers(child, results));
    } else {
        results.push(parent);
    }
}

function featureSortPriority(layer) {
    const ft = layer._featureType || '';
    if (ft === 'LSR') return 0;
    const type = layer._geojsonFeature?.properties?.type || ft;
    if (['Hit', 'Miss', 'NoTag', 'OtherImpact'].includes(type)) return 1;
    if (['Considerable', 'Catastrophic', 'Limited'].includes(type)) return 2;
    return 3;
}

function getFeaturesAtPoint(latlng) {
    const results = [];
    const pt = L.latLng(latlng);
    const seenContent = new Set();
    activeLayers.forEach(group => {
        if (!map.hasLayer(group)) return;
        const leaves = [];
        collectLeafLayers(group, leaves);
        leaves.forEach(leaf => {
            if (!leaf._popupContent) return;
            if (seenContent.has(leaf._popupContent)) return;
            if (layerContainsPoint(leaf, pt)) {
                seenContent.add(leaf._popupContent);
                results.push(leaf);
            }
        });
    });
    results.sort((a, b) => featureSortPriority(a) - featureSortPriority(b));
    return results;
}

function _raycastPointInRing(pt, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const xi = ring[i][0], yi = ring[i][1];
        const xj = ring[j][0], yj = ring[j][1];
        if (((yi > pt[1]) !== (yj > pt[1])) &&
            (pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi)) {
            inside = !inside;
        }
    }
    return inside;
}

function _pointInPolygonCoords(pt, coords) {
    if (!_raycastPointInRing(pt, coords[0])) return false;
    for (let i = 1; i < coords.length; i++) {
        if (_raycastPointInRing(pt, coords[i])) return false;
    }
    return true;
}

function _pointInGeoJSONGeometry(pt, geom) {
    if (!geom || !geom.type) return false;
    if (geom.type === 'Polygon') {
        return _pointInPolygonCoords(pt, geom.coordinates);
    }
    if (geom.type === 'MultiPolygon') {
        return geom.coordinates.some(poly => _pointInPolygonCoords(pt, poly));
    }
    return false;
}

function layerContainsPoint(layer, latlng) {
    if (layer instanceof L.CircleMarker && !(layer instanceof L.Circle)) {
        const center = layer.getLatLng();
        const pixelDist = map.latLngToContainerPoint(latlng).distanceTo(map.latLngToContainerPoint(center));
        return pixelDist <= (layer.getRadius() + 4);
    }
    if (typeof layer.getBounds === 'function') {
        const bounds = layer.getBounds();
        if (!bounds.isValid() || !bounds.contains(latlng)) return false;
    }
    const feat = layer._geojsonFeature;
    if (feat?.geometry) {
        const gtype = feat.geometry.type;
        if (gtype === 'Point' || gtype === 'MultiPoint') {
            return false;
        }
        return _pointInGeoJSONGeometry([latlng.lng, latlng.lat], feat.geometry);
    }
    if (typeof layer.getLatLngs === 'function') {
        return true;
    }
    return false;
}

let _stackedPopup = null;
let _highlightLayer = null;

function clearHighlight() {
    if (_highlightLayer) {
        map.removeLayer(_highlightLayer);
        _highlightLayer = null;
    }
}

function highlightFeature(layer) {
    clearHighlight();
    const target = layer._parentLayer || layer;
    const highlightStyle = {
        color: '#facc15', weight: 4, opacity: 1,
        fillOpacity: 0.25, fillColor: '#facc15',
        dashArray: null, interactive: false
    };

    if (target instanceof L.CircleMarker && !(target instanceof L.Circle)) {
        _highlightLayer = L.circleMarker(target.getLatLng(), {
            radius: target.getRadius() + 6,
            color: '#facc15', weight: 3, opacity: 1,
            fillColor: '#facc15', fillOpacity: 0.3,
            interactive: false
        }).addTo(map);
    } else if (typeof target.getLatLngs === 'function') {
        _highlightLayer = L.polygon(target.getLatLngs(), highlightStyle).addTo(map);
    } else if (typeof target.eachLayer === 'function') {
        const group = L.featureGroup();
        target.eachLayer(sub => {
            if (sub instanceof L.CircleMarker && !(sub instanceof L.Circle)) {
                L.circleMarker(sub.getLatLng(), {
                    radius: sub.getRadius() + 6,
                    color: '#facc15', weight: 3, opacity: 1,
                    fillColor: '#facc15', fillOpacity: 0.3,
                    interactive: false
                }).addTo(group);
            } else if (typeof sub.getLatLngs === 'function') {
                L.polygon(sub.getLatLngs(), highlightStyle).addTo(group);
            }
        });
        if (group.getLayers().length) {
            group.addTo(map);
            _highlightLayer = group;
        }
    }
}

function openStackedPopup(latlng) {
    const hits = getFeaturesAtPoint(latlng);
    if (!hits.length) return;

    if (hits.length === 1) {
        clearHighlight();
        L.popup({ maxWidth: 300 })
            .setLatLng(latlng)
            .setContent(hits[0]._popupContent)
            .openOn(map);
        highlightFeature(hits[0]);
        map.once('popupclose', clearHighlight);
        return;
    }

    let currentIdx = 0;

    function buildContent() {
        return `<div class="stacked-popup-wrap">
            <div class="stacked-popup-body">${hits[currentIdx]._popupContent}</div>
            <div class="popup-pager">
                <button class="popup-pager-btn" data-dir="prev" ${currentIdx === 0 ? 'disabled' : ''}>&lsaquo;</button>
                <span class="popup-pager-pos">${currentIdx + 1} / ${hits.length}</span>
                <button class="popup-pager-btn" data-dir="next" ${currentIdx === hits.length - 1 ? 'disabled' : ''}>&rsaquo;</button>
            </div>
        </div>`;
    }

    if (_stackedPopup) { map.closePopup(_stackedPopup); }
    clearHighlight();

    _stackedPopup = L.popup({ maxWidth: 300, minWidth: 200, closeButton: true })
        .setLatLng(latlng)
        .setContent(buildContent())
        .openOn(map);

    highlightFeature(hits[0]);

    _stackedPopup.on('remove', () => {
        _stackedPopup = null;
        clearHighlight();
    });

    function onPagerClick(ev) {
        const btn = ev.target.closest('[data-dir]');
        if (!btn) return;
        ev.stopPropagation();
        ev.preventDefault();
        if (btn.dataset.dir === 'prev' && currentIdx > 0) currentIdx--;
        else if (btn.dataset.dir === 'next' && currentIdx < hits.length - 1) currentIdx++;
        else return;
        const wrapper = _stackedPopup?.getElement()?.querySelector('.leaflet-popup-content');
        if (wrapper) wrapper.innerHTML = buildContent();
        highlightFeature(hits[currentIdx]);
    }

    const pane = map.getPane('popupPane');
    pane.addEventListener('click', onPagerClick);
    _stackedPopup.on('remove', () => {
        pane.removeEventListener('click', onPagerClick);
    });
}
