/**
 * LifeLane Driver Emergency Portal – Client Application
 * Handles authentication, emergency preemption requests, live GPS streaming,
 * simulated corridor demo replay, voice prompts, and PWA registration.
 */

// ── Configuration & State ───────────────────────────────────────────────────
const API_BASE = window.location.origin; // Automatically connects to backend host
let currentVehicle = {
  id: localStorage.getItem('lifelane_vehicle_id') || 'AMB-108',
  type: localStorage.getItem('lifelane_vehicle_type') || 'ambulance',
};

let activeEmergency = {
  requestId: null,
  destination: null,
  plan: null,
  isActive: false,
};

let voiceEnabled = localStorage.getItem('lifelane_voice') !== 'false';
let gpsIntervalId = null;
let hudTimerIntervalId = null;
let currentEtaSeconds = 45;

// Simulated GPS path approaching Central Junction C (28.6139, 77.2090) from South to North
const DEMO_WAYPOINTS = [
  { lat: 28.6010, lon: 77.2090, speed: 48, label: 'Origin Approach South', dist: 1430 },
  { lat: 28.6045, lon: 77.2090, speed: 52, label: 'En-route S2C', dist: 1040 },
  { lat: 28.6080, lon: 77.2090, speed: 54, label: 'Approaching South Radius', dist: 650 },
  { lat: 28.6110, lon: 77.2090, speed: 55, label: 'Signal Preemption Zone', dist: 320 },
  { lat: 28.6128, lon: 77.2090, speed: 52, label: 'Junction Stop Line Ahead', dist: 120 },
  { lat: 28.6145, lon: 77.2090, speed: 50, label: 'Cleared Junction C', dist: 0 },
  { lat: 28.6180, lon: 77.2090, speed: 52, label: 'North Corridor Outbound', dist: 450 },
  { lat: 28.6220, lon: 77.2090, speed: 49, label: 'Arriving at Destination', dist: 900 },
];
let demoWaypointIndex = 0;

// ── PWA Service Worker Registration ─────────────────────────────────────────
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js').catch((err) => {
      console.warn('PWA Service Worker registration failed:', err);
    });
  });
}

// ── Voice Prompts (Web Speech API) ──────────────────────────────────────────
function speakPrompt(text) {
  if (!voiceEnabled || !window.speechSynthesis) return;
  try {
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.05;
    utterance.pitch = 1.0;
    utterance.lang = 'en-US';
    window.speechSynthesis.speak(utterance);
  } catch (e) {
    console.warn('Speech synthesis error:', e);
  }
}

function toggleVoice() {
  voiceEnabled = !voiceEnabled;
  localStorage.setItem('lifelane_voice', voiceEnabled);
  updateVoiceUI();
  if (voiceEnabled) {
    speakPrompt('Voice guidance enabled.');
  }
}

function updateVoiceUI() {
  const statusEl = document.getElementById('voiceStatusText');
  const btnEl = document.getElementById('voiceToggleBtn');
  if (statusEl) statusEl.innerText = voiceEnabled ? 'ON' : 'OFF';
  if (btnEl) btnEl.style.color = voiceEnabled ? '#10b981' : '#94a3b8';
}

// ── Screen Navigation ───────────────────────────────────────────────────────
function showScreen(screenId) {
  document.querySelectorAll('.screen-view').forEach((el) => {
    el.classList.remove('active');
  });
  const target = document.getElementById(screenId);
  if (target) target.classList.add('active');
}

// ── Login / Profile Handling ────────────────────────────────────────────────
function handleLogin(e) {
  e.preventDefault();
  const idInput = document.getElementById('vehicleIdInput').value.trim();
  const typeSelect = document.getElementById('vehicleTypeSelect').value;

  if (!idInput) {
    alert('Please enter a valid Vehicle ID.');
    return;
  }

  currentVehicle.id = idInput;
  currentVehicle.type = typeSelect;

  localStorage.setItem('lifelane_vehicle_id', currentVehicle.id);
  localStorage.setItem('lifelane_vehicle_type', currentVehicle.type);

  updateCockpitUI();
  showScreen('cockpitScreen');
}

function setPreset(id, type) {
  document.getElementById('vehicleIdInput').value = id;
  document.getElementById('vehicleTypeSelect').value = type;
  handleVehicleTypeChange();
}

function handleVehicleTypeChange() {
  const type = document.getElementById('vehicleTypeSelect').value;
  const isPrivate = type.startsWith('private_');
  const pvtBox = document.getElementById('privateVerificationGroup');
  if (pvtBox) {
    pvtBox.style.display = isPrivate ? 'block' : 'none';
  }
}

function updateCockpitUI() {
  document.getElementById('cockpitVehicleId').innerText = currentVehicle.id;

  const typeLabels = {
    ambulance: 'Ambulance • Official Fleet',
    fire: 'Fire Brigade • Official Fleet',
    police: 'Police Escort • Official Fleet',
    private_medical: 'Private Vehicle • Medical Alert',
    private_fire: 'Private Vehicle • Fire Alert',
  };
  const typeIcons = {
    ambulance: '🚑',
    fire: '🚒',
    police: '🚓',
    private_medical: '🚗 (Medical)',
    private_fire: '🚗 (Fire)',
  };

  document.getElementById('cockpitVehicleType').innerText = typeLabels[currentVehicle.type] || currentVehicle.type;
  document.getElementById('cockpitVehicleIcon').innerText = typeIcons[currentVehicle.type] || '🚑';
}

// ── Modal Handling ──────────────────────────────────────────────────────────
function openEmergencyModal() {
  const isPrivate = currentVehicle.type.startsWith('private_');
  const pvtBox = document.getElementById('privateVerificationGroup');
  if (pvtBox) pvtBox.style.display = isPrivate ? 'block' : 'none';

  // Pre-select emergency kind based on vehicle type
  const radios = document.getElementsByName('emergencyKind');
  if (currentVehicle.type.includes('fire')) {
    radios[1].checked = true;
  } else {
    radios[0].checked = true;
  }

  document.getElementById('emergencyModal').classList.add('open');
}

function closeEmergencyModal() {
  document.getElementById('emergencyModal').classList.remove('open');
}

// ── Submit Emergency Request ────────────────────────────────────────────────
async function submitEmergencyRequest(e) {
  e.preventDefault();
  const dest = document.getElementById('destinationSelect').value;
  const refId = document.getElementById('referenceIdInput').value.trim();

  // Reset demo waypoint to beginning of approach
  demoWaypointIndex = 0;
  const initialCoord = DEMO_WAYPOINTS[0];

  const payload = {
    vehicle_id: currentVehicle.id,
    vehicle_type: currentVehicle.type,
    lat: initialCoord.lat,
    lon: initialCoord.lon,
    destination: dest,
    reference_id: refId || null,
    speed_kmh: 50.0,
  };

  const confirmBtn = document.getElementById('dispatchConfirmBtn');
  confirmBtn.innerText = 'Verifying credentials...';
  confirmBtn.disabled = true;

  try {
    const res = await fetch(`${API_BASE}/emergency/request`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    const data = await res.json();

    if (!res.ok || data.status === 'REJECTED') {
      alert(`Emergency Priority Denied:\n${data.reason || data.detail || 'Authorization failed'}`);
      speakPrompt('Emergency request rejected. Please verify credentials.');
      return;
    }

    // Success!
    activeEmergency.requestId = data.request_id;
    activeEmergency.destination = dest;
    activeEmergency.plan = data.corridor_plan;
    activeEmergency.isActive = true;

    closeEmergencyModal();
    launchActiveCorridorHUD(data);

  } catch (err) {
    alert(`Backend unreachable at ${API_BASE}.\nCheck that the FastAPI server is running on port 8000.`);
  } finally {
    confirmBtn.innerText = '⚡ CLEAR CORRIDOR NOW';
    confirmBtn.disabled = false;
  }
}

// ── Active Corridor HUD & GPS Streaming ─────────────────────────────────────
function launchActiveCorridorHUD(response) {
  showScreen('activeCorridorScreen');

  const plan = response.corridor_plan;
  const firstJunction = plan && plan.junction_schedules ? plan.junction_schedules[0] : null;

  currentEtaSeconds = firstJunction ? Math.round(firstJunction.eta_seconds) : 45;
  document.getElementById('etaSecondsDisplay').innerText = currentEtaSeconds;
  document.getElementById('nextJunctionName').innerText = firstJunction ? firstJunction.junction_name : 'Central Crossing (Junction C)';
  document.getElementById('approachRoadText').innerText = `Approach: ${firstJunction ? firstJunction.approach_road.toUpperCase() : 'SOUTH'} ROAD`;
  document.getElementById('corridorDestinationText').innerText = activeEmergency.destination;
  document.getElementById('verifyMessageText').innerText = response.verification_message || 'Verified • Green Wave Active';

  const planInfo = plan.is_private ? 'Private Corridor (25s green, 1 junction)' : 'Full Priority Corridor (45s green)';
  document.getElementById('corridorPlanInfo').innerText = planInfo;

  // Voice announcement
  speakPrompt(`Emergency request verified. Green corridor active for ${activeEmergency.destination}. Next junction in ${currentEtaSeconds} seconds.`);

  // Start 1-second countdown timer for HUD display
  if (hudTimerIntervalId) clearInterval(hudTimerIntervalId);
  hudTimerIntervalId = setInterval(updateHudTimer, 1000);

  // Start 3-second GPS telemetry stream to backend
  if (gpsIntervalId) clearInterval(gpsIntervalId);
  gpsIntervalId = setInterval(streamLiveGps, 3000);
}

function updateHudTimer() {
  if (currentEtaSeconds > 0) {
    currentEtaSeconds -= 1;
    document.getElementById('etaSecondsDisplay').innerText = currentEtaSeconds;

    // Milestone voice prompts
    if (currentEtaSeconds === 30) {
      speakPrompt('Approaching Central Crossing in 30 seconds. Approach road South is green.');
    } else if (currentEtaSeconds === 10) {
      speakPrompt('Junction arrival imminent. Signal clear.');
    }
  }

  // Visual progress bar
  const totalEta = 45;
  const elapsed = Math.max(0, totalEta - currentEtaSeconds);
  const pct = Math.min(100, Math.round((elapsed / totalEta) * 100));
  const fillEl = document.getElementById('corridorProgressFill');
  if (fillEl) fillEl.style.width = `${pct}%`;
}

async function streamLiveGps() {
  if (!activeEmergency.isActive || !activeEmergency.requestId) return;

  const useDemoGps = document.getElementById('demoGpsToggle').checked;
  let lat = 28.6080;
  let lon = 77.2090;
  let speed = 50.0;
  let distText = '320m';

  if (useDemoGps) {
    // Advance waypoint in simulated corridor path
    demoWaypointIndex = Math.min(demoWaypointIndex + 1, DEMO_WAYPOINTS.length - 1);
    const wp = DEMO_WAYPOINTS[demoWaypointIndex];
    lat = wp.lat;
    lon = wp.lon;
    speed = wp.speed;
    distText = `${wp.dist}m`;

    document.getElementById('distRemainingText').innerText = `Distance: ${distText}`;
    document.getElementById('liveSpeedText').innerText = `Speed: ${speed} km/h`;

    if (demoWaypointIndex === 5) {
      // Reached junction center!
      speakPrompt('Central Crossing cleared. Proceeding outbound.');
    }
  } else if ('geolocation' in navigator) {
    navigator.geolocation.getCurrentPosition((pos) => {
      lat = pos.coords.latitude;
      lon = pos.coords.longitude;
      speed = pos.coords.speed ? Math.round(pos.coords.speed * 3.6) : 50.0;
    });
  }

  try {
    const res = await fetch(`${API_BASE}/emergency/${activeEmergency.requestId}/location`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lat, lon, speed_kmh: speed }),
    });

    if (res.ok) {
      const data = await res.json();
      const updatedSchedules = data.refreshed_plan && data.refreshed_plan.junction_schedules;
      if (updatedSchedules && updatedSchedules.length > 0) {
        currentEtaSeconds = Math.max(0, Math.round(updatedSchedules[0].eta_seconds));
      }
      document.getElementById('gpsStreamStatus').innerText = `Live GPS: ${lat.toFixed(4)}, ${lon.toFixed(4)} (${speed} km/h)`;
    }
  } catch (e) {
    document.getElementById('gpsStreamStatus').innerText = 'Offline / Retrying stream...';
  }
}

// ── Cancel Emergency Corridor ───────────────────────────────────────────────
async function cancelActiveCorridor() {
  if (!activeEmergency.requestId) return;

  const btn = document.getElementById('cancelCorridorBtn');
  btn.innerText = 'Cancelling corridor...';

  try {
    await fetch(`${API_BASE}/emergency/${activeEmergency.requestId}/cancel`, {
      method: 'POST',
    });
  } catch (e) {
    console.warn('Cancel fetch failed:', e);
  }

  // Cleanup timers
  if (gpsIntervalId) clearInterval(gpsIntervalId);
  if (hudTimerIntervalId) clearInterval(hudTimerIntervalId);

  activeEmergency.isActive = false;
  activeEmergency.requestId = null;

  speakPrompt('Emergency corridor cancelled. Normal adaptive signal timing restored.');
  alert('Emergency corridor cancelled.\nNormal signal timing has been restored on all junctions.');

  btn.innerHTML = '🛑 CANCEL EMERGENCY CORRIDOR<small>Restores normal signal cycle immediately</small>';
  showScreen('cockpitScreen');
}

// ── Initialization on Load ──────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  document.getElementById('vehicleIdInput').value = currentVehicle.id;
  document.getElementById('vehicleTypeSelect').value = currentVehicle.type;
  handleVehicleTypeChange();
  updateCockpitUI();
  updateVoiceUI();

  // Voice toggle button handler
  const voiceBtn = document.getElementById('voiceToggleBtn');
  if (voiceBtn) voiceBtn.addEventListener('click', toggleVoice);

  // If already logged in previously, jump to cockpit
  if (currentVehicle.id) {
    showScreen('cockpitScreen');
  }
});
