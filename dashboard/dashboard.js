/**
 * LifeLane Traffic Police Command Console – Frontend Script
 * Manages Leaflet map visualization, live WebSocket telemetry,
 * corridor overrides, and emergency audit history.
 */

const API_BASE = window.location.origin;
const WS_PROTOCOL = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTOCOL}//${window.location.host}/ws/emergency`;

let map = null;
let junctionMarkers = {};
let vehicleMarker = null;
let corridorPolyline = null;
let activeCorridors = [];

// ── Known Reference Coordinates ─────────────────────────────────────────────
const JUNCTIONS_GEO = {
  J_CENTRAL: { name: 'Central Crossing (Junction C)', lat: 28.6139, lon: 77.2090, tls: 'C' },
  J_NORTH:   { name: 'North Radial Junction',        lat: 28.6190, lon: 77.2090, tls: 'N_TL' },
  J_SOUTH:   { name: 'South Radial Junction',        lat: 28.6088, lon: 77.2090, tls: 'S_TL' },
  J_EAST:    { name: 'East Radial Junction',         lat: 28.6139, lon: 77.2150, tls: 'E_TL' },
  J_WEST:    { name: 'West Radial Junction',         lat: 28.6139, lon: 77.2030, tls: 'W_TL' },
};

const DESTINATIONS_GEO = {
  'City General Hospital': { name: '🏥 City General Hospital', lat: 28.6250, lon: 77.2090 },
  'Metro Trauma Center':    { name: '🏥 Metro Trauma Center',    lat: 28.6040, lon: 77.2090 },
  'Central Fire Station':   { name: '🚒 Central Fire Station',   lat: 28.6139, lon: 77.2200 },
  'North Sector Fire Unit': { name: '🚒 North Fire Unit',        lat: 28.6250, lon: 77.2050 },
};

// ── 1. Leaflet Map Initialization ───────────────────────────────────────────
function initMap() {
  map = L.map('map', {
    center: [28.6139, 77.2090],
    zoom: 15,
    zoomControl: true,
  });

  // Dark theme CartoDB basemap
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; OpenStreetMap',
    maxZoom: 19,
  }).addTo(map);

  // Draw arterial road grid lines connecting radial junctions
  const roadNetwork = [
    [JUNCTIONS_GEO.J_SOUTH, JUNCTIONS_GEO.J_CENTRAL],
    [JUNCTIONS_GEO.J_CENTRAL, JUNCTIONS_GEO.J_NORTH],
    [JUNCTIONS_GEO.J_WEST, JUNCTIONS_GEO.J_CENTRAL],
    [JUNCTIONS_GEO.J_CENTRAL, JUNCTIONS_GEO.J_EAST],
    [JUNCTIONS_GEO.J_NORTH, DESTINATIONS_GEO['City General Hospital']],
    [JUNCTIONS_GEO.J_SOUTH, DESTINATIONS_GEO['Metro Trauma Center']],
    [JUNCTIONS_GEO.J_EAST, DESTINATIONS_GEO['Central Fire Station']],
  ];

  roadNetwork.forEach((segment) => {
    L.polyline(
      [[segment[0].lat, segment[0].lon], [segment[1].lat, segment[1].lon]],
      { color: '#2e3e68', weight: 6, opacity: 0.8, lineCap: 'round' }
    ).addTo(map);
  });

  // Render Junction Nodes
  Object.entries(JUNCTIONS_GEO).forEach(([id, junc]) => {
    const isCentral = id === 'J_CENTRAL';
    const icon = L.divIcon({
      className: 'custom-junc-icon',
      html: `
        <div id="junc-badge-${id}" class="junction-marker-icon ${isCentral ? 'central' : ''}">
          🚦 ${junc.name.split(' (')[0]}
          <div id="q-stats-${id}" class="approach-queue-bubble">N: 4 | S: 2 | E: 1 | W: 2</div>
        </div>
      `,
      iconSize: [160, 40],
      iconAnchor: [80, 20],
    });

    const m = L.marker([junc.lat, junc.lon], { icon }).addTo(map);
    m.bindPopup(`<strong>${junc.name}</strong><br>Traffic Signal Controller ID: <code>${junc.tls}</code>`);
    junctionMarkers[id] = m;
  });

  // Render Destination Facilities
  Object.values(DESTINATIONS_GEO).forEach((dest) => {
    const icon = L.divIcon({
      className: 'dest-icon',
      html: `<div style="background:#1e293b;border:2px solid #3b82f6;border-radius:6px;padding:3px 6px;color:#fff;font-size:10px;font-weight:700;white-space:nowrap;">${dest.name}</div>`,
      iconSize: [140, 26],
      iconAnchor: [70, 13],
    });
    L.marker([dest.lat, dest.lon], { icon }).addTo(map);
  });
}

// ── 2. Update Map with Active Corridor & Live Vehicle ───────────────────────
function updateMapCorridor(corridors) {
  if (!map) return;

  // Reset junction styling
  Object.keys(JUNCTIONS_GEO).forEach((id) => {
    const el = document.getElementById(`junc-badge-${id}`);
    if (el) el.classList.remove('preempted');
  });

  if (!corridors || corridors.length === 0) {
    if (corridorPolyline) {
      map.removeLayer(corridorPolyline);
      corridorPolyline = null;
    }
    if (vehicleMarker) {
      map.removeLayer(vehicleMarker);
      vehicleMarker = null;
    }
    return;
  }

  // Visualise first active corridor
  const c = corridors[0];
  const plan = c.plan || {};
  const schedules = plan.junction_schedules || [];

  // 1. Draw glowing cyan corridor path
  const pathCoords = [];
  if (c.lat && c.lon) pathCoords.push([c.lat, c.lon]);

  schedules.forEach((s) => {
    const jId = s.junction_id;
    if (JUNCTIONS_GEO[jId]) {
      pathCoords.push([JUNCTIONS_GEO[jId].lat, JUNCTIONS_GEO[jId].lon]);
      const badge = document.getElementById(`junc-badge-${jId}`);
      if (badge) badge.classList.add('preempted');
    }
  });

  if (DESTINATIONS_GEO[c.destination]) {
    pathCoords.push([DESTINATIONS_GEO[c.destination].lat, DESTINATIONS_GEO[c.destination].lon]);
  }

  if (corridorPolyline) map.removeLayer(corridorPolyline);
  corridorPolyline = L.polyline(pathCoords, {
    color: '#06b6d4',
    weight: 7,
    opacity: 0.9,
    dashArray: '8, 8',
  }).addTo(map);

  // 2. Render / update live vehicle marker
  const vehLat = c.lat || 28.6080;
  const vehLon = c.lon || 77.2090;
  const vehIconEmoji = c.vehicle_type && c.vehicle_type.includes('fire') ? '🚒' : '🚑';

  if (!vehicleMarker) {
    const icon = L.divIcon({
      className: 'live-veh-wrap',
      html: `<div class="vehicle-live-marker">${vehIconEmoji}</div>`,
      iconSize: [36, 36],
      iconAnchor: [18, 18],
    });
    vehicleMarker = L.marker([vehLat, vehLon], { icon }).addTo(map);
  } else {
    vehicleMarker.setLatLng([vehLat, vehLon]);
  }
}

// ── 3. WebSocket Real-Time Connection ───────────────────────────────────────
function initWebSocket() {
  const wsBadge = document.getElementById('wsStatusBadge');
  const wsText = document.getElementById('wsStatusText');

  try {
    const ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      wsBadge.className = 'status-pill connected';
      wsText.innerText = 'WebSocket: LIVE';
    };

    ws.onmessage = (event) => {
      try {
        const msg = jsonParseSafe(event.data);
        if (msg) handleWsMessage(msg);
      } catch (e) {
        console.warn('WS message parse error:', e);
      }
    };

    ws.onclose = () => {
      wsBadge.className = 'status-pill';
      wsText.innerText = 'Polling (Fallback)';
      setTimeout(initWebSocket, 4000); // Auto-reconnect
    };

    ws.onerror = () => {
      ws.close();
    };
  } catch (e) {
    wsBadge.className = 'status-pill';
    wsText.innerText = 'Polling (Fallback)';
  }
}

function handleWsMessage(msg) {
  if (msg.event === 'INITIAL_SNAPSHOT') {
    activeCorridors = msg.active_corridors || [];
    renderActiveCorridors(activeCorridors);
    updateMapCorridor(activeCorridors);
  } else if (msg.event === 'CORRIDOR_CREATED') {
    activeCorridors.push(msg.data);
    renderActiveCorridors(activeCorridors);
    updateMapCorridor(activeCorridors);
    fetchEmergencyLogs();
  } else if (msg.event === 'CORRIDOR_UPDATED') {
    const idx = activeCorridors.findIndex((c) => c.request_id === msg.request_id);
    if (idx !== -1) {
      activeCorridors[idx].lat = msg.lat;
      activeCorridors[idx].lon = msg.lon;
      activeCorridors[idx].plan = msg.updated_plan;
      renderActiveCorridors(activeCorridors);
      updateMapCorridor(activeCorridors);
    }
  } else if (msg.event === 'CORRIDOR_CANCELLED' || msg.event === 'POLICE_OVERRIDE') {
    activeCorridors = activeCorridors.filter((c) => c.request_id !== msg.target_id && c.request_id !== msg.request_id);
    renderActiveCorridors(activeCorridors);
    updateMapCorridor(activeCorridors);
    fetchEmergencyLogs();
  }
}

function jsonParseSafe(str) {
  try { return JSON.parse(str); } catch (_) { return null; }
}

// ── 4. Polling & Data Retrieval ─────────────────────────────────────────────
async function fetchActiveCorridors() {
  try {
    const res = await fetch(`${API_BASE}/emergency/active`);
    if (res.ok) {
      const data = await res.json();
      activeCorridors = data.corridors || [];
      renderActiveCorridors(activeCorridors);
      updateMapCorridor(activeCorridors);
    }
  } catch (e) {
    console.warn('Failed to fetch active corridors:', e);
  }
}

async function fetchEmergencyLogs() {
  const statusFilter = document.getElementById('statusFilter').value;
  const url = statusFilter ? `${API_BASE}/emergency/log?status=${statusFilter}` : `${API_BASE}/emergency/log`;

  try {
    const res = await fetch(url);
    if (res.ok) {
      const data = await res.json();
      renderLogTable(data.logs || []);
      updateKpis(data.logs || []);
    }
  } catch (e) {
    console.warn('Failed to fetch logs:', e);
  }
}

// ── 5. Render Active Corridors Panel ────────────────────────────────────────
function renderActiveCorridors(corridors) {
  const listEl = document.getElementById('activeCorridorsList');
  const countLabel = document.getElementById('activeCountLabel');
  const kpiBadge = document.getElementById('kpiActiveBadge');

  if (countLabel) countLabel.innerText = `${corridors.length} emergency vehicle${corridors.length === 1 ? '' : 's'} in transit`;
  if (kpiBadge) kpiBadge.innerText = `${corridors.length} Active`;

  if (!corridors || corridors.length === 0) {
    listEl.innerHTML = `
      <div class="empty-state">
        <span class="empty-icon">✅</span>
        <p><strong>All Junctions Clear</strong></p>
        <p class="empty-sub">Signals operating in normal adaptive control.</p>
      </div>
    `;
    return;
  }

  listEl.innerHTML = corridors.map((c) => {
    const plan = c.plan || {};
    const schedules = plan.junction_schedules || [];
    const firstJunc = schedules[0] || {};
    const nextEta = firstJunc.eta_seconds !== undefined ? `${Math.round(firstJunc.eta_seconds)}s` : 'Imminent';

    const scheduleRows = schedules.map((s) => `
      <div class="junction-schedule-row">
        <span class="sched-junc-name">${s.junction_name}</span>
        <span class="sched-approach">Approach: ${s.approach_road}</span>
        <span class="sched-eta">ETA: ${Math.round(s.eta_seconds)}s</span>
      </div>
    `).join('');

    const icon = c.vehicle_type && c.vehicle_type.includes('fire') ? '🚒' : '🚑';

    return `
      <div class="corridor-item-card">
        <div class="corridor-card-header">
          <div class="corridor-veh-title">
            <span>${icon}</span>
            <span>${c.vehicle_id}</span>
            <span class="corridor-type-tag">${c.vehicle_type.replace('_', ' ')}</span>
          </div>
          <span class="corridor-eta-pill">Next ETA: ${nextEta}</span>
        </div>

        <div class="corridor-route-strip">
          ${scheduleRows}
        </div>

        <div class="corridor-card-footer">
          <span class="corridor-dest-text">Dest: <strong>${c.destination}</strong></span>
          <button class="police-cancel-btn" onclick="executePoliceOverride('${c.request_id}')">
            🛑 POLICE CANCEL
          </button>
        </div>
      </div>
    `;
  }).join('');
}

// ── 6. Render Log Table & KPI Update ────────────────────────────────────────
function renderLogTable(logs) {
  const tbody = document.getElementById('logTableBody');

  if (!logs || logs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" class="loading-td">No emergency records found.</td></tr>`;
    return;
  }

  tbody.innerHTML = logs.map((l) => {
    const timeStr = new Date(l.created_at).toLocaleTimeString();
    const isOngoing = l.status === 'ACTIVE';

    const actionCell = isOngoing
      ? `<button class="table-btn" onclick="executePoliceOverride('${l.id}')">Override</button>`
      : `<span style="color:var(--text-muted);font-size:11px;">Completed</span>`;

    const note = l.rejection_reason || (l.cancelled_by ? `Cancelled by ${l.cancelled_by}` : 'Preempted successfully');

    return `
      <tr>
        <td><code>${l.id}</code></td>
        <td>${timeStr}</td>
        <td><strong>${l.vehicle_id}</strong></td>
        <td>${l.vehicle_type}</td>
        <td>${l.destination}</td>
        <td>${l.reference_id || '—'}</td>
        <td><span class="status-chip ${l.status}">${l.status}</span></td>
        <td style="max-width:220px;word-break:break-word;font-size:12px;">${note}</td>
        <td>${actionCell}</td>
      </tr>
    `;
  }).join('');
}

function updateKpis(logs) {
  const kpiCount = document.getElementById('kpiEmergenciesCount');
  if (kpiCount) kpiCount.innerText = logs.length;
}

// ── 7. Police Override Handlers ─────────────────────────────────────────────
async function executePoliceOverride(emergencyId) {
  if (!confirm(`Confirm Traffic Police Manual Override:\nCancel priority corridor ${emergencyId} immediately?`)) {
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/police/override`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        officer_id: 'TP-INSPECTOR-12',
        action: 'cancel_corridor',
        target_emergency_id: emergencyId,
        reason: 'Traffic Police Manual Override via Web Console',
      }),
    });

    if (res.ok) {
      alert(`Priority corridor ${emergencyId} cancelled. Signals restored to normal adaptive mode.`);
      fetchActiveCorridors();
      fetchEmergencyLogs();
    }
  } catch (e) {
    alert('Failed to send override request to backend.');
  }
}

async function confirmMasterOverride() {
  if (!confirm('⚠️ CRITICAL POLICE ACTION:\nCancel ALL active emergency corridors and force normal timing on all junctions?')) {
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/police/override`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        officer_id: 'TP-INSPECTOR-12',
        action: 'restore_all',
        target_emergency_id: null,
        reason: 'Master Police Panic Override',
      }),
    });

    if (res.ok) {
      alert('All active corridors cancelled. All signals restored.');
      fetchActiveCorridors();
      fetchEmergencyLogs();
    }
  } catch (e) {
    alert('Failed to execute master override.');
  }
}

// ── Initialization on Load ──────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  initMap();
  initWebSocket();
  fetchActiveCorridors();
  fetchEmergencyLogs();

  // Periodic fallback polling every 3 seconds
  setInterval(() => {
    fetchActiveCorridors();
    fetchEmergencyLogs();
  }, 3000);
});
