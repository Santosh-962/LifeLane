"""
LifeLane Emergency Backend – FastAPI Server
===========================================
Handles emergency priority requests, verification, route & ETA calculation,
dynamic corridor scheduling, GPS tracking, and police overrides.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from emergency_backend.corridor import build_corridor_plan
from emergency_backend.db import (
    DB_PATH,
    get_db,
    init_db,
    log_police_override,
    save_emergency_request,
    set_emergency_status,
    update_emergency_location_and_plan,
)
from emergency_backend.verifier import verify_emergency_request
from emergency_backend.gemini_assistant import (
    EmergencyDescriptionResponse,
    FacilityAlertResponse,
    IncidentSummaryResponse,
    generate_facility_alert,
    generate_police_incident_summary,
    parse_emergency_text,
)


# ── Pydantic Request Models ──────────────────────────────────────────────────

class DescribeEmergencyRequest(BaseModel):
    text: str = Field(..., example="car fire near the hospital, driver injured")


class FacilityAlertRequest(BaseModel):
    vehicle_id: str = Field(..., example="AMB-108")
    emergency_type: str = Field(..., example="FIRE_MEDICAL")
    patient_type: str = Field(..., example="Driver with severe burns and smoke inhalation")
    destination: str = Field(..., example="City General Hospital")
    eta_seconds: int = Field(..., example=45)


class EmergencyCreateRequest(BaseModel):
    vehicle_id: str = Field(..., example="AMB-108")
    vehicle_type: str = Field(
        ...,
        description="One of: ambulance, fire, police, private_medical, private_fire",
        example="ambulance",
    )
    lat: float = Field(..., example=28.6080)
    lon: float = Field(..., example=77.2090)
    destination: str = Field(..., example="City General Hospital")
    reference_id: Optional[str] = Field(
        None,
        description="Mandatory for private vehicles (112 Incident ID or Hospital Name)",
        example="112-DEL-2024-8891",
    )
    speed_kmh: Optional[float] = Field(50.0, example=50.0)


class LocationUpdateRequest(BaseModel):
    lat: float = Field(..., example=28.6110)
    lon: float = Field(..., example=77.2090)
    speed_kmh: Optional[float] = Field(50.0, example=55.0)


class PoliceOverrideRequest(BaseModel):
    officer_id: str = Field(..., example="TRAFFIC-INSPECTOR-42")
    action: str = Field(
        ...,
        description="Action type: cancel_corridor, restore_all, force_green",
        example="cancel_corridor",
    )
    target_emergency_id: Optional[str] = Field(None, example="EMG-8f43ad")
    reason: Optional[str] = Field(None, example="Major congestion on secondary artery / false alarm")


# ── WebSocket Broadcast Manager ──────────────────────────────────────────────

class DashboardWebSocketManager:
    """Broadcaster for connected signal-controllers and web dashboards."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict[str, Any]):
        payload = json.dumps(message)
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_text(payload)
            except Exception:
                dead.append(connection)
        for d in dead:
            if d in self.active_connections:
                self.active_connections.remove(d)


ws_manager = DashboardWebSocketManager()

# In-memory store of active corridors for ultra-fast signal-controller polling
ACTIVE_CORRIDORS: dict[str, dict[str, Any]] = {}


# ── Lifespan Startup / Shutdown ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)
    yield


app = FastAPI(
    title="LifeLane Emergency Corridor Service",
    description="Emergency vehicle preemption, verification, and dynamic green wave corridor coordinator.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Serve Driver PWA Web Application ─────────────────────────────────────────
driver_app_dir = Path(__file__).resolve().parent.parent / "driver_app"
if driver_app_dir.exists():
    app.mount("/driver", StaticFiles(directory=str(driver_app_dir), html=True), name="driver_app")

# ── Serve Traffic Police Command Dashboard ───────────────────────────────────
dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard"
if dashboard_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")

@app.get("/", include_in_schema=False)
def root_redirect():
    """Redirect root visit to driver PWA interface."""
    return RedirectResponse(url="/driver/")


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/emergency/request", status_code=200)
async def request_emergency_corridor(req: EmergencyCreateRequest):
    """Raise an emergency corridor request.

    Verifies vehicle credentials, calculates route and ETAs,
    schedules green wave (ETA - 40s), logs to SQLite, and notifies the signal controller.
    """
    request_id = f"EMG-{uuid.uuid4().hex[:8].upper()}"
    conn = get_db(DB_PATH)

    # 1. Verification
    is_valid, verify_msg = verify_emergency_request(
        db_conn=conn,
        vehicle_id=req.vehicle_id,
        vehicle_type=req.vehicle_type,
        reference_id=req.reference_id,
    )
    conn.close()

    if not is_valid:
        # Save rejection in audit log
        save_emergency_request(
            request_id=request_id,
            vehicle_id=req.vehicle_id,
            vehicle_type=req.vehicle_type,
            status="REJECTED",
            lat=req.lat,
            lon=req.lon,
            destination=req.destination,
            reference_id=req.reference_id,
            plan=None,
            rejection_reason=verify_msg,
            db_path=DB_PATH,
        )
        return JSONResponse(
            status_code=400,
            content={
                "request_id": request_id,
                "status": "REJECTED",
                "verified": False,
                "reason": verify_msg,
            },
        )

    # 2. Build Corridor Plan & ETA calculation
    speed = req.speed_kmh if req.speed_kmh and req.speed_kmh > 5.0 else 50.0
    plan = build_corridor_plan(
        current_lat=req.lat,
        current_lon=req.lon,
        destination=req.destination,
        vehicle_type=req.vehicle_type,
        speed_kmh=speed,
    )

    # 3. Save accepted request to SQLite
    save_emergency_request(
        request_id=request_id,
        vehicle_id=req.vehicle_id,
        vehicle_type=req.vehicle_type,
        status="ACTIVE",
        lat=req.lat,
        lon=req.lon,
        destination=req.destination,
        reference_id=req.reference_id,
        plan=plan,
        rejection_reason=None,
        db_path=DB_PATH,
    )

    # 4. Save to in-memory active corridors
    active_entry = {
        "request_id": request_id,
        "vehicle_id": req.vehicle_id,
        "vehicle_type": req.vehicle_type,
        "status": "ACTIVE",
        "lat": req.lat,
        "lon": req.lon,
        "speed_kmh": speed,
        "destination": req.destination,
        "reference_id": req.reference_id,
        "plan": plan,
    }
    ACTIVE_CORRIDORS[request_id] = active_entry

    # 5. Broadcast to dashboard and signal-control module
    await ws_manager.broadcast({
        "event": "CORRIDOR_CREATED",
        "data": active_entry,
    })

    return {
        "request_id": request_id,
        "status": "ACTIVE",
        "verified": True,
        "verification_message": verify_msg,
        "corridor_plan": plan,
    }


@app.put("/emergency/{request_id}/location")
async def update_emergency_location(request_id: str, loc: LocationUpdateRequest):
    """Update vehicle GPS position in real-time, recalculating ETAs and the green corridor schedule."""
    conn = get_db(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM emergency_log WHERE id = ?", (request_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Emergency request '{request_id}' not found.")

    if row["status"] != "ACTIVE":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot update location: Request '{request_id}' is in status '{row['status']}'.",
        )

    speed = loc.speed_kmh if loc.speed_kmh and loc.speed_kmh > 5.0 else 50.0
    updated_plan = build_corridor_plan(
        current_lat=loc.lat,
        current_lon=loc.lon,
        destination=row["destination"],
        vehicle_type=row["vehicle_type"],
        speed_kmh=speed,
    )

    update_emergency_location_and_plan(
        request_id=request_id,
        lat=loc.lat,
        lon=loc.lon,
        speed_kmh=speed,
        updated_plan=updated_plan,
        db_path=DB_PATH,
    )

    if request_id in ACTIVE_CORRIDORS:
        ACTIVE_CORRIDORS[request_id]["lat"] = loc.lat
        ACTIVE_CORRIDORS[request_id]["lon"] = loc.lon
        ACTIVE_CORRIDORS[request_id]["speed_kmh"] = speed
        ACTIVE_CORRIDORS[request_id]["plan"] = updated_plan

    # Broadcast updated plan
    await ws_manager.broadcast({
        "event": "CORRIDOR_UPDATED",
        "request_id": request_id,
        "lat": loc.lat,
        "lon": loc.lon,
        "speed_kmh": speed,
        "updated_plan": updated_plan,
    })

    return {
        "request_id": request_id,
        "status": "UPDATED",
        "refreshed_plan": updated_plan,
    }


@app.post("/emergency/{request_id}/cancel")
async def cancel_emergency_request(request_id: str):
    """Driver cancels the emergency request; restores normal signal timing on all junctions."""
    conn = get_db(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT status FROM emergency_log WHERE id = ?", (request_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Emergency request '{request_id}' not found.")

    set_emergency_status(request_id, "CANCELLED", cancelled_by="driver", db_path=DB_PATH)
    ACTIVE_CORRIDORS.pop(request_id, None)

    await ws_manager.broadcast({
        "event": "CORRIDOR_CANCELLED",
        "request_id": request_id,
        "action": "restore_normal_timing",
    })

    return {
        "request_id": request_id,
        "status": "CANCELLED",
        "message": "Emergency corridor cancelled. Normal adaptive signal timing restored.",
    }


@app.post("/police/override")
async def police_override(override: PoliceOverrideRequest):
    """Traffic police manual override.

    Allows cancelling any active corridor or restoring all signals immediately.
    """
    log_police_override(
        officer_id=override.officer_id,
        action=override.action,
        target_id=override.target_emergency_id,
        reason=override.reason,
        db_path=DB_PATH,
    )

    if override.action in ("cancel_corridor", "cancel") and override.target_emergency_id:
        set_emergency_status(
            override.target_emergency_id,
            "POLICE_OVERRIDDEN",
            cancelled_by=f"officer:{override.officer_id}",
            db_path=DB_PATH,
        )
        ACTIVE_CORRIDORS.pop(override.target_emergency_id, None)

    elif override.action in ("restore_all", "cancel_all"):
        conn = get_db(DB_PATH)
        conn.execute(
            "UPDATE emergency_log SET status = 'POLICE_OVERRIDDEN', cancelled_by = ? WHERE status = 'ACTIVE'",
            (f"officer:{override.officer_id}",),
        )
        conn.commit()
        conn.close()
        ACTIVE_CORRIDORS.clear()

    # Broadcast police override to signals
    await ws_manager.broadcast({
        "event": "POLICE_OVERRIDE",
        "officer_id": override.officer_id,
        "action": override.action,
        "target_id": override.target_emergency_id,
        "reason": override.reason,
    })

    return {
        "status": "OVERRIDDEN",
        "officer_id": override.officer_id,
        "action": override.action,
        "message": f"Police override '{override.action}' executed successfully and logged.",
    }


@app.get("/emergency/log")
def get_emergency_log(status: Optional[str] = None, limit: int = Query(50, le=200)):
    """Retrieve emergency request history, with optional status filter."""
    conn = get_db(DB_PATH)
    cur = conn.cursor()

    if status:
        cur.execute(
            "SELECT * FROM emergency_log WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status.upper(), limit),
        )
    else:
        cur.execute(
            "SELECT * FROM emergency_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    rows = cur.fetchall()
    conn.close()

    results = []
    for r in rows:
        item = dict(r)
        if item.get("plan_json"):
            try:
                item["plan"] = json.loads(item["plan_json"])
            except Exception:
                item["plan"] = None
        else:
            item["plan"] = None
        results.append(item)

    return {"count": len(results), "logs": results}


@app.get("/emergency/active")
def get_active_corridors():
    """Retrieve all currently active green-wave corridors for signal controllers & dashboard."""
    return {"active_count": len(ACTIVE_CORRIDORS), "corridors": list(ACTIVE_CORRIDORS.values())}


@app.get("/vehicles")
def list_registered_vehicles():
    """List registered emergency service vehicles."""
    conn = get_db(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT vehicle_id, vehicle_type, department, plate_number, status FROM vehicles ORDER BY vehicle_id")
    rows = cur.fetchall()
    conn.close()
    return {"registered_vehicles": [dict(r) for r in rows]}


# ── Gemini Assistant Endpoints ───────────────────────────────────────────────

@app.post("/emergency/describe", response_model=EmergencyDescriptionResponse)
def describe_emergency_call(req: DescribeEmergencyRequest):
    """Parse free-text emergency descriptions or voice notes into structured JSON triage data.

    Uses Gemini API with deterministic rule-based keyword fallback.
    SAFETY: Gemini output is informative only; signal changes are handled by deterministic logic.
    """
    return parse_emergency_text(req.text)


@app.post("/emergency/alert-message", response_model=FacilityAlertResponse)
def create_facility_alert_message(req: FacilityAlertRequest):
    """Generate a concise operational dispatch alert for receiving hospital triage or fire stations.

    Uses Gemini API with structured fallback template.
    """
    return generate_facility_alert(
        vehicle_id=req.vehicle_id,
        emergency_type=req.emergency_type,
        patient_type=req.patient_type,
        destination=req.destination,
        eta_seconds=req.eta_seconds,
    )


@app.get("/emergency/{request_id}/summary", response_model=IncidentSummaryResponse)
def get_police_incident_summary(request_id: str):
    """Generate a formal plain-language incident summary from SQLite logs for traffic police supervisors."""
    conn = get_db(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM emergency_log WHERE id = ?", (request_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Incident '{request_id}' not found in log.")

    return generate_police_incident_summary(dict(row))


@app.websocket("/ws/emergency")
async def websocket_emergency_feed(websocket: WebSocket):
    """Live WebSocket feed for dashboard visualization and signal controllers."""
    await ws_manager.connect(websocket)
    try:
        # Send initial active snapshot
        await websocket.send_text(json.dumps({
            "event": "INITIAL_SNAPSHOT",
            "active_corridors": list(ACTIVE_CORRIDORS.values()),
        }))
        while True:
            # Keep alive and listen for client heartbeats
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
