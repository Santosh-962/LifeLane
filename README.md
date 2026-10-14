# LifeLane – Zone-Based Vehicle Counter

Detects, tracks, and counts vehicles in up to 4 user-defined polygon zones
using **YOLOv8 + ByteTrack**.  For each zone it computes:

| Metric | Description |
|--------|-------------|
| `current_count` | Vehicles currently inside the zone |
| `total_unique` | Cumulative unique vehicles (no double-counting) |
| `weighted_count` | PCU-weighted count (bike=0.5, car=1, auto=0.75, bus/truck=2.5) |
| `queue_length_m` | Extent of halted vehicles, converted to metres |
| `avg_wait_sec` | Mean waiting time of vehicles moving below the speed threshold |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note:** The first run will auto-download `yolov8n.pt` (~6 MB).

### 2. Prepare your video

Place a traffic video file (e.g. `traffic.mp4`) in this directory, or
change `video_source` in `zone_config.yaml`.  Set `video_source: 0` for
your webcam.

### 3. Configure zones

**Option A — interactive picker** (recommended first time):
```bash
python zone_counter.py --pick-zones
```
Left-click to add vertices, press **Enter** to finish each zone, press **Q**
when done.  The coordinates are saved back to `zone_config.yaml`.

**Option B — edit the YAML directly:**
Open `zone_config.yaml` and set the `polygon` coordinates for each zone.

### 4. Calibrate `pixels_per_metre`

Measure a known distance on the road (e.g. lane width ≈ 3.5 m).  Count how
many pixels that distance spans in the video frame.  Set
`pixels_per_metre` = pixels / metres.

### 5. Run

```bash
python zone_counter.py
```

Or with a custom config:
```bash
python zone_counter.py --config my_config.yaml
```

### 6. Outputs

| Output | Location |
|--------|----------|
| Annotated video | `output.mp4` |
| JSON Lines log | `counts.jsonl` |
| Live REST API | `http://localhost:8001/counts` |

Press **Q** in the video window or **Ctrl+C** in the terminal to stop.

---

## JSONL Format

Each line in `counts.jsonl` is a JSON object:

```json
{
  "timestamp_sec": 4.0,
  "zones": {
    "North": {
      "current_count": 5,
      "total_unique": 12,
      "weighted_count": 6.5,
      "queue_length_m": 23.4,
      "avg_wait_sec": 8.2,
      "halted_count": 4,
      "by_class": {"car": 3, "motorcycle": 1, "bus": 1}
    },
    ...
  }
}
```

---

## API

```bash
curl http://localhost:8001/counts
```

Returns the latest snapshot in the same format as one JSONL line.

---

## Auto-Rickshaw Detection

The default COCO model does not include auto-rickshaws.  To detect them:

1. Fine-tune YOLOv8 on a dataset that includes three-wheelers (e.g.
   [Indian Driving Dataset](https://idd.insaan.iiit.ac.in/)).
2. Update `class_map` in `zone_config.yaml` with the new class ID:
   ```yaml
   class_map:
     2: "car"
     3: "motorcycle"
     5: "bus"
     7: "truck"
     80: "auto_rickshaw"
   ```

---

## File Structure

```
zone_counter.py                 ← YOLOv8 vehicle detection & zone metrics
zone_config.yaml                ← Zone counter configuration
adaptive_controller.py          ← Adaptive signal controller with camera/SUMO bridge
run_baseline.py                 ← SUMO baseline TraCI runner
lifelane_controller.py          ← Extended TraCI controller with backend preemption & flush
evaluate_benchmark.py           ← 10-seed Monte Carlo evaluation & 4 failure-case tests
sumo_config/                    ← 4-way 2-lane SUMO network & mixed routes (with ambulance vType)
emergency_backend/
  ├── db.py                     ← SQLite schema (vehicles, emergency_log, police overrides)
  ├── verifier.py               ← Official registry check & private vehicle verification
  ├── corridor.py               ← Routing, Haversine ETA, ETA - 40s green wave scheduler
  ├── gemini_assistant.py       ← Gemini natural language triage, alerting & police summaries
  └── main.py                   ← FastAPI REST API & WebSocket service
driver_app/                     ← Mobile-friendly PWA for drivers (3s GPS stream & demo replay)
dashboard/                      ← Traffic Police Command Center dashboard (Leaflet map & override)
tests/
  ├── test_emergency_backend.py ← Unit tests for verification rules & ETA/corridor logic
  └── test_gemini_assistant.py  ← Unit tests for Gemini triage & fallback classifiers
evaluation_results.csv          ← Raw metrics across 10 randomized traffic arrival seeds
failure_cases_results.json      ← Security & failure-case audit results
presentation_charts.html        ← Interactive presentation slides & charts for pitch deck
EVALUATION_REPORT.md            ← Comprehensive statistical evaluation report
requirements.txt                ← Dependencies
README.md                       ← Project documentation
```


---

## LifeLane Emergency Priority Backend

The emergency backend manages priority green corridors for ambulances, fire engines, and verified private emergencies.

### Starting the Emergency Service

```bash
uvicorn emergency_backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Interactive OpenAPI documentation is available at: `http://localhost:8000/docs`

### API Endpoints & Example Requests

#### 1. Request Emergency Corridor
```bash
# Registered Ambulance (Auto-verified against registry)
curl -X POST http://localhost:8000/emergency/request \
  -H "Content-Type: application/json" \
  -d '{
    "vehicle_id": "AMB-108",
    "vehicle_type": "ambulance",
    "lat": 28.6080,
    "lon": 77.2090,
    "destination": "City General Hospital",
    "speed_kmh": 50.0
  }'

# Verified Private Medical Alert (Requires 112 ID or hospital name)
curl -X POST http://localhost:8000/emergency/request \
  -H "Content-Type: application/json" \
  -d '{
    "vehicle_id": "PVT-9921",
    "vehicle_type": "private_medical",
    "lat": 28.6080,
    "lon": 77.2090,
    "destination": "Metro Trauma Center",
    "reference_id": "112-DEL-2024-8891"
  }'
```

#### 2. Update Live GPS Location (Refreshes ETAs & Green Schedules)
```bash
curl -X PUT http://localhost:8000/emergency/EMG-XXXXXXXX/location \
  -H "Content-Type: application/json" \
  -d '{"lat": 28.6110, "lon": 77.2090, "speed_kmh": 55.0}'
```

#### 3. Cancel Emergency Corridor (Driver)
```bash
curl -X POST http://localhost:8000/emergency/EMG-XXXXXXXX/cancel
```

#### 4. Police Manual Override (Traffic Police)
```bash
curl -X POST http://localhost:8000/police/override \
  -H "Content-Type: application/json" \
  -d '{
    "officer_id": "TP-INSPECTOR-12",
    "action": "cancel_corridor",
    "target_emergency_id": "EMG-XXXXXXXX",
    "reason": "Congestion clearance completed"
  }'
```

#### 5. Query Audit Logs
```bash
curl http://localhost:8000/emergency/log?status=ACTIVE
```

### Running Unit Tests

```bash
python -m unittest tests/test_emergency_backend.py -v
```

---

## LifeLane Driver Emergency Web App (PWA)

A mobile-friendly, installable Progressive Web App (PWA) designed for emergency vehicle drivers and verified private motorists.

### Accessing the Web App

When the FastAPI server is running (`uvicorn emergency_backend.main:app --port 8000`), navigate to:
👉 **`http://localhost:8000/`** or **`http://localhost:8000/driver/`**

On mobile (Chrome / Safari / Edge), tap **"Add to Home Screen"** or **"Install App"** to run it as a standalone, full-screen cockpit display.

### Features
1. **Vehicle Authentication**:
   - Quick-select presets: `Ambulance #108`, `Fire Engine #101`, or `Private Medical`.
   - Remembers vehicle credentials in local storage.
2. **One Large Red "EMERGENCY" Button**:
   - High-contrast, tactile, pulsing circular button designed for fast one-tap activation.
3. **Dispatch Modal**:
   - Choose Emergency Type (*Medical Emergency* or *Fire Rescue*).
   - Select Destination (*City General Hospital*, *Metro Trauma Center*, *Central Fire Station*).
   - **Mandatory 112 Verification**: Private vehicles require a valid 112 Reference ID or hospital token.
4. **Live Corridor HUD & Countdown**:
   - Displays real-time ETA countdown to the next junction.
   - Shows approach arm (`Approach: SOUTH ROAD`) and signal status (`SIGNAL: GREEN WAVE ACTIVE`).
   - Dynamic distance and speed telemetry.
5. **Live GPS Telemetry (Every 3 Seconds)**:
   - Streams live coordinates to `PUT /emergency/{id}/location`.
6. **Demo Mode (Simulated Corridor Replay)**:
   - Toggle switch on the cockpit screen: replays a realistic GPS trajectory approaching the central junction from 1,400m south at 50 km/h, triggering live preemption and countdowns on the backend and SUMO simulation.
7. **Voice Prompts (Web Speech API)**:
   - Voice guidance announces approval, approach alerts (*"Approaching Central Crossing in 30 seconds"*), and corridor clearance.

---

## Traffic Police Command Center Dashboard

An operations console for traffic management and police supervisors.

### Accessing the Dashboard

Open your browser at:
👉 **`http://localhost:8000/dashboard/`**

### Features
1. **Interactive Leaflet Map**:
   - Displays all radial junctions (`J_CENTRAL`, `J_NORTH`, `J_SOUTH`, `J_EAST`, `J_WEST`) and major emergency facilities.
   - Live approach queue badges per arm (North, South, East, West).
   - Dynamic cyan glowing corridor polyline highlighting active priority routes.
   - Real-time animated emergency vehicle marker tracking incoming GPS coordinates.
2. **Real-Time Active Corridors Panel**:
   - Displays vehicle ID, category tag, and destination.
   - Step-by-step junction route with individual ETA countdowns and approach arms.
   - Individual **"Police Cancel"** button to abort specific corridors.
3. **Master Panic Override**:
   - Prominent **"⚠️ RESET / CANCEL ALL CORRIDORS"** button in top bar to immediately flush all signals back to normal timing.
4. **Emergency Dispatch & Verification Audit Log**:
   - Persistent SQLite history showing all active, cancelled, overridden, and rejected dispatches.
   - Includes verification rationale (112 tokens, hospital references, or rejection reasons).
   - Filter by status dropdown.
5. **Real-Time KPI Cards**:
   - **Ambulance Time Saved**: `-72.3%` / `47.0s` saved per run.
   - **Average Waiting Time Saved**: `-31.8%` from adaptive queue-weighted green allocation.
   - **Cross-Street Delay Added**: `+1.8s` momentary pause, mitigated by post-clearance flush.
   - **Total Emergencies Handled**: Live counter with active corridor badge.
6. **Dual Telemetry Feed**:
   - Streams live events via WebSocket (`ws://localhost:8000/ws/emergency`) with seamless fallback to 3-second HTTP polling.

---

## Gemini AI Dispatch & Incident Analysis

LifeLane integrates the Google Gemini API (`gemini-2.5-flash`) to triage emergency descriptions, generate hospital dispatch alerts, and draft formal police incident reports.

> **Safety Rule**: Gemini is used strictly for natural language triage and reporting. It **NEVER** decides signal changes or phase timings. All signal preemption is controlled by the deterministic safety state machine.

### Gemini Endpoints

#### 1. Free-Text & Voice Note Triage (`POST /emergency/describe`)
Converts unstructured voice notes or 911 calls into strict, validated JSON triage data:
```bash
curl -X POST http://localhost:8000/emergency/describe \
  -H "Content-Type: application/json" \
  -d '{"text": "car fire near the hospital, driver injured"}'
```
**Response**:
```json
{
  "emergency_type": "FIRE_MEDICAL",
  "severity": "CRITICAL",
  "units_needed": ["fire_engine", "ambulance"],
  "location_text": "near the hospital",
  "confidence": 0.95,
  "source": "gemini"
}
```

#### 2. Hospital / Fire Station Alert Generator (`POST /emergency/alert-message`)
Generates high-urgency operational dispatch notices under 25 words:
```bash
curl -X POST http://localhost:8000/emergency/alert-message \
  -H "Content-Type: application/json" \
  -d '{
    "vehicle_id": "AMB-108",
    "emergency_type": "FIRE_MEDICAL",
    "patient_type": "Driver with severe burns and smoke inhalation",
    "destination": "City General Hospital",
    "eta_seconds": 45
  }'
```

#### 3. Plain-Language Police Incident Summary (`GET /emergency/{id}/summary`)
Generates a formal 2-sentence executive incident report for traffic police supervisors directly from the SQLite audit record:
```bash
curl http://localhost:8000/emergency/EMG-XXXXXXXX/summary
```

### Deterministic Keyword Fallback
If `GEMINI_API_KEY` is omitted or if the network is down, LifeLane automatically and transparently switches to its built-in rule-based keyword triage parser, ensuring zero downtime for emergency operations.

### Running Gemini Unit Tests
```bash
python -m unittest tests/test_gemini_assistant.py -v
```




