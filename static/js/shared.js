/**
 * shared.js — Common utilities used by both FHO Verification and IBW Validation pages.
 * Must be loaded AFTER Leaflet and BEFORE page-specific scripts (app.js / ibw.js).
 */

// ── HTML escaping ────────────────────────────────────────────────────────────
function escapeHtml(str) {
    if (str == null) return '';
    const s = String(str);
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Error toast ──────────────────────────────────────────────────────────────
function showError(message) {
    const existing = document.getElementById('error-notification');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.id = 'error-notification';
    toast.className = 'error-toast';
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'polite');
    toast.innerHTML = `
        <svg class="error-toast-icon" viewBox="0 0 20 20" fill="currentColor">
            <path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.168 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 6a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 6zm0 9a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd" />
        </svg>
        <span class="error-toast-message">${escapeHtml(message)}</span>
        <button type="button" class="btn-close" aria-label="Close"></button>`;
    document.body.appendChild(toast);

    const closeBtn = toast.querySelector('.btn-close');
    if (closeBtn) {
        closeBtn.addEventListener('click', () => {
            toast.classList.add('dismissing');
            toast.addEventListener('animationend', () => toast.remove());
        });
    }
    setTimeout(() => {
        if (toast.parentNode) {
            toast.classList.add('dismissing');
            toast.addEventListener('animationend', () => toast.remove());
        }
    }, 5000);
}

// ── Animated value transition ────────────────────────────────────────────────
function animateValue(el, endVal, duration = 400, suffix = '') {
    if (!el) return;
    if (el._animFrameId) cancelAnimationFrame(el._animFrameId);
    const parsed = parseFloat(el.textContent);
    const startVal = Number.isFinite(parsed) ? parsed : 0;
    const finalText = (Number.isInteger(endVal) ? endVal : endVal.toFixed(1)) + suffix;
    if (startVal === endVal) {
        el.textContent = finalText;
        return;
    }
    const startTime = performance.now();
    const step = (now) => {
        const progress = Math.min((now - startTime) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = startVal + (endVal - startVal) * eased;
        el.textContent = (Number.isInteger(endVal) ? Math.round(current) : current.toFixed(1)) + suffix;
        if (progress < 1) { el._animFrameId = requestAnimationFrame(step); }
        else { el._animFrameId = null; }
    };
    el._animFrameId = requestAnimationFrame(step);
}

// ── Date formatting ──────────────────────────────────────────────────────────
/** Breakpoints for verification-rate coloring (0–100 scale). `goalPct` defaults to 70. */
function verificationRateBreakpoints(goalPct) {
    const g = Number.isFinite(goalPct) && goalPct > 0 ? goalPct : 70;
    const w = Math.max(0, Math.min(g - 1, Math.round((g * 4) / 7)));
    return { successAt: g, warnAt: w };
}

/** Inline color for stat text (matches progress/badge semantics). */
function colorForVerificationRate(ratePct, goalPct) {
    const { successAt, warnAt } = verificationRateBreakpoints(goalPct);
    if (ratePct >= successAt) return 'var(--success-color)';
    if (ratePct >= warnAt) return 'var(--warning-color)';
    return 'var(--danger-color)';
}

/** Bootstrap progress-bar classes for a 0–100 rate. */
function progressBarClassesForRate(ratePct, goalPct) {
    const { successAt, warnAt } = verificationRateBreakpoints(goalPct);
    const base = 'progress-bar';
    if (ratePct >= successAt) return `${base} bg-success`;
    if (ratePct >= warnAt) return `${base} bg-warning`;
    return `${base} bg-danger`;
}

/** Badge classes for a 0–100 rate. */
function badgeClassesForRate(ratePct, goalPct) {
    const { successAt, warnAt } = verificationRateBreakpoints(goalPct);
    const base = 'badge';
    if (ratePct >= successAt) return `${base} bg-success`;
    if (ratePct >= warnAt) return `${base} bg-warning`;
    return `${base} bg-danger`;
}

function formatDateStr(val) {
    if (!val) return 'Unknown';
    try {
        // Server sends UTC-naive ISO strings; append Z if no timezone indicator present
        let iso = String(val);
        if (iso.length > 10 && !iso.endsWith('Z') && !iso.includes('+') && !/[-+]\d{2}:\d{2}$/.test(iso)) {
            iso += 'Z';
        }
        const d = new Date(iso);
        if (isNaN(d)) return String(val);
        return d.toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC';
    } catch { return String(val); }
}

// ── Segmented button groups ──────────────────────────────────────────────────
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

// ── Theme toggle ─────────────────────────────────────────────────────────────
const _sunIcon = '<path stroke-linecap="round" stroke-linejoin="round" d="M12 3v2.25m6.364.386l-1.591 1.591M21 12h-2.25m-.386 6.364l-1.591-1.591M12 18.75V21m-4.773-4.227l-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z" />';
const _moonIcon = '<path stroke-linecap="round" stroke-linejoin="round" d="M21.752 15.002A9.718 9.718 0 0112.478 3.34a9.72 9.72 0 109.274 11.662z" />';

/**
 * Creates a dark base-map tile layer (CARTO Dark Matter).
 * Each page needs its own Leaflet TileLayer instance.
 */
function createDarkBaseMap() {
    return L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        subdomains: 'abcd',
        maxZoom: 20,
        minZoom: 0
    });
}

/**
 * Initialise the light/dark theme toggle.
 * @param {L.Map} map - The Leaflet map instance
 * @param {L.TileLayer} lightBaseMap - The light tile layer (already added to map)
 * @param {L.TileLayer} darkBaseMap - The dark tile layer (created via createDarkBaseMap)
 */
function initThemeToggle(map, lightBaseMap, darkBaseMap) {
    function applyTheme(theme) {
        document.documentElement.dataset.theme = theme;
        const icon = document.getElementById('themeIcon');
        if (icon) icon.innerHTML = theme === 'dark' ? _sunIcon : _moonIcon;

        // Only remove the known base layers to preserve satellite/other tile layers
        if (map.hasLayer(lightBaseMap)) map.removeLayer(lightBaseMap);
        if (map.hasLayer(darkBaseMap)) map.removeLayer(darkBaseMap);

        if (theme === 'dark') {
            darkBaseMap.addTo(map);
        } else {
            lightBaseMap.addTo(map);
        }
    }

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
