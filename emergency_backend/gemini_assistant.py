"""
LifeLane AI Assistant – Gemini API Integration & Rule-Based Fallback
===================================================================
Provides natural language perception, facility alert messaging, and
incident summarization using the Google Gemini API with guaranteed
rule-based keyword fallbacks.

STRICT SAFETY GUARANTEE:
  Gemini NEVER decides traffic signal changes or phase timings.
  All signal control is governed strictly by the deterministic,
  safety-checked rule-based controller.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, List, Literal, Optional
from pydantic import BaseModel, Field

# Target Gemini model
GEMINI_MODEL = "gemini-2.5-flash"


# ── Pydantic Output Schemas ──────────────────────────────────────────────────

class EmergencyDescriptionResponse(BaseModel):
    emergency_type: Literal[
        "MEDICAL",
        "FIRE",
        "FIRE_MEDICAL",
        "POLICE_ESCORT",
        "TRAFFIC_ACCIDENT",
        "OTHER",
    ] = Field(..., description="Classified emergency category")
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] = Field(
        ..., description="Urgency / severity triage level"
    )
    units_needed: List[str] = Field(
        ..., description="Recommended emergency response units (e.g. ambulance, fire_engine)"
    )
    location_text: str = Field(
        ..., description="Extracted landmark, street, or destination location reference"
    )
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    source: Literal["gemini", "keyword_fallback"] = Field(
        default="gemini", description="Whether processed via Gemini AI or rule-based fallback"
    )


class FacilityAlertResponse(BaseModel):
    alert_message: str = Field(..., description="Short, concise operational dispatch message")
    destination: str
    eta_seconds: int
    source: Literal["gemini", "keyword_fallback"]


class IncidentSummaryResponse(BaseModel):
    incident_id: str
    vehicle_id: str
    summary_text: str = Field(..., description="Plain-language incident summary for traffic police")
    source: Literal["gemini", "keyword_fallback"]


# ── 1. Voice Note / Free Text Description Parsing ────────────────────────────

def parse_emergency_text(text: str) -> EmergencyDescriptionResponse:
    """Parse unstructured emergency dispatch text using Gemini API with keyword fallback."""
    cleaned = (text or "").strip()
    if not cleaned:
        return _keyword_fallback_describe("Unspecified emergency call")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if api_key:
        try:
            return _call_gemini_describe(cleaned, api_key)
        except Exception as e:
            # Fall back gracefully on network or API failure
            res = _keyword_fallback_describe(cleaned)
            res.source = "keyword_fallback"
            return res

    # If no API key configured, use deterministic keyword fallback
    return _keyword_fallback_describe(cleaned)


def _call_gemini_describe(text: str, api_key: str) -> EmergencyDescriptionResponse:
    """Invoke Gemini API with structured JSON output schema."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"

    prompt = f"""
You are a 911 / 112 emergency dispatch triage assistant for the LifeLane smart traffic signal system.
Analyze the following emergency text or transcribed driver voice note:
"{text}"

Extract and classify into strict JSON matching this structure:
{{
  "emergency_type": "MEDICAL" | "FIRE" | "FIRE_MEDICAL" | "POLICE_ESCORT" | "TRAFFIC_ACCIDENT" | "OTHER",
  "severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW",
  "units_needed": ["ambulance" and/or "fire_engine" and/or "police_patrol"],
  "location_text": "extracted location or landmark, or 'Unknown location'"
}}
Do NOT output markdown code blocks. Output raw JSON only.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=4.0) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        raw_content = body["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw_content)

        return EmergencyDescriptionResponse(
            emergency_type=parsed.get("emergency_type", "OTHER"),
            severity=parsed.get("severity", "HIGH"),
            units_needed=parsed.get("units_needed", ["ambulance"]),
            location_text=parsed.get("location_text", "Unknown location"),
            confidence=0.96,
            source="gemini",
        )


def _keyword_fallback_describe(text: str) -> EmergencyDescriptionResponse:
    """Deterministic keyword-based triage classifier (failsafe)."""
    lower = text.lower()

    has_fire = any(k in lower for k in ["fire", "flame", "smoke", "burn", "blaze", "explosion"])
    has_medical = any(
        k in lower
        for k in [
            "injured", "injury", "blood", "bleed", "cardiac", "heart", "unconscious",
            "patient", "stroke", "casualty", "trauma", "doctor", "ambulance", "hospital",
        ]
    )
    has_accident = any(k in lower for k in ["accident", "crash", "collision", "overturned"])

    # Extract location snippet via regex ("near X", "at X", "on X")
    loc_match = re.search(r"\b(?:near|at|on|towards|outside)\s+([a-zA-Z0-9\s,]+)", text, re.IGNORECASE)
    location_text = loc_match.group(0).strip() if loc_match else "Location not specified"

    if has_fire and has_medical:
        return EmergencyDescriptionResponse(
            emergency_type="FIRE_MEDICAL",
            severity="CRITICAL",
            units_needed=["fire_engine", "ambulance"],
            location_text=location_text,
            confidence=0.88,
            source="keyword_fallback",
        )
    elif has_fire:
        return EmergencyDescriptionResponse(
            emergency_type="FIRE",
            severity="HIGH",
            units_needed=["fire_engine"],
            location_text=location_text,
            confidence=0.85,
            source="keyword_fallback",
        )
    elif has_medical:
        sev = "CRITICAL" if any(w in lower for w in ["unconscious", "cardiac", "severe", "fatal"]) else "HIGH"
        return EmergencyDescriptionResponse(
            emergency_type="MEDICAL",
            severity=sev,
            units_needed=["ambulance"],
            location_text=location_text,
            confidence=0.85,
            source="keyword_fallback",
        )
    elif has_accident:
        return EmergencyDescriptionResponse(
            emergency_type="TRAFFIC_ACCIDENT",
            severity="HIGH",
            units_needed=["ambulance", "police_patrol"],
            location_text=location_text,
            confidence=0.80,
            source="keyword_fallback",
        )

    return EmergencyDescriptionResponse(
        emergency_type="OTHER",
        severity="MEDIUM",
        units_needed=["ambulance"],
        location_text=location_text,
        confidence=0.60,
        source="keyword_fallback",
    )


# ── 2. Facility Alert Message Generator ─────────────────────────────────────

def generate_facility_alert(
    vehicle_id: str,
    emergency_type: str,
    patient_type: str,
    destination: str,
    eta_seconds: int,
) -> FacilityAlertResponse:
    """Generate a concise operational dispatch alert for hospital triage or fire stations."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if api_key:
        try:
            return _call_gemini_facility_alert(
                vehicle_id, emergency_type, patient_type, destination, eta_seconds, api_key
            )
        except Exception:
            pass

    # Fallback template
    return _fallback_facility_alert(vehicle_id, patient_type, destination, eta_seconds)


def _call_gemini_facility_alert(
    vehicle_id: str,
    emergency_type: str,
    patient_type: str,
    destination: str,
    eta_seconds: int,
    api_key: str,
) -> FacilityAlertResponse:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"

    prompt = f"""
Generate a short, urgent operational alert message (strictly under 25 words) for the receiving triage team at {destination}.
Details:
- Vehicle: {vehicle_id}
- ETA: {eta_seconds} seconds
- Incident/Patient: {patient_type} ({emergency_type})
Format: Crisp, professional trauma dispatch message.
Do NOT use markdown code blocks or quotes.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=3.5) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        msg = body["candidates"][0]["content"]["parts"][0]["text"].strip()
        return FacilityAlertResponse(
            alert_message=msg,
            destination=destination,
            eta_seconds=eta_seconds,
            source="gemini",
        )


def _fallback_facility_alert(
    vehicle_id: str,
    patient_type: str,
    destination: str,
    eta_seconds: int,
) -> FacilityAlertResponse:
    msg = f"🚨 INBOUND ALERT: Unit {vehicle_id} arriving at {destination} in {eta_seconds}s. Patient Profile: {patient_type}. Prepare trauma bay."
    return FacilityAlertResponse(
        alert_message=msg,
        destination=destination,
        eta_seconds=eta_seconds,
        source="keyword_fallback",
    )


# ── 3. Plain-Language Police Incident Summary ────────────────────────────────

def generate_police_incident_summary(
    incident_row: dict[str, Any],
) -> IncidentSummaryResponse:
    """Generate a clean, professional incident report for police supervisors from log data."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    inc_id = incident_row.get("id", "UNKNOWN")
    v_id = incident_row.get("vehicle_id", "N/A")

    if api_key:
        try:
            return _call_gemini_police_summary(incident_row, api_key)
        except Exception:
            pass

    return _fallback_police_summary(incident_row)


def _call_gemini_police_summary(
    incident_row: dict[str, Any],
    api_key: str,
) -> IncidentSummaryResponse:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"

    prompt = f"""
You are writing an official Traffic Police incident summary report for an emergency signal preemption dispatch.
Incident Log Data:
- ID: {incident_row.get('id')}
- Vehicle: {incident_row.get('vehicle_id')} ({incident_row.get('vehicle_type')})
- Destination: {incident_row.get('destination')}
- Status: {incident_row.get('status')}
- Created At: {incident_row.get('created_at')}
- Verification Note: {incident_row.get('rejection_reason') or incident_row.get('reference_id') or 'Verified Fleet'}
- Override / Cancel: {incident_row.get('cancelled_by') or 'None'}

Write a concise 2-sentence formal incident report summarizing the dispatch, corridor status, and adherence to signal safety.
Do NOT use markdown.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=4.0) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        summary = body["candidates"][0]["content"]["parts"][0]["text"].strip()
        return IncidentSummaryResponse(
            incident_id=incident_row.get("id", "UNKNOWN"),
            vehicle_id=incident_row.get("vehicle_id", "N/A"),
            summary_text=summary,
            source="gemini",
        )


def _fallback_police_summary(incident_row: dict[str, Any]) -> IncidentSummaryResponse:
    v_id = incident_row.get("vehicle_id", "N/A")
    v_type = incident_row.get("vehicle_type", "emergency")
    dest = incident_row.get("destination", "facility")
    status = incident_row.get("status", "COMPLETED")
    cancelled = incident_row.get("cancelled_by")

    if status == "REJECTED":
        reason = incident_row.get("rejection_reason", "Credentials failed")
        text = f"Incident {incident_row.get('id')}: Priority corridor request for {v_type} {v_id} was REJECTED ({reason}). Normal adaptive signal timing was maintained."
    elif cancelled:
        text = f"Incident {incident_row.get('id')}: Emergency priority for {v_type} {v_id} en-route to {dest} was cancelled by {cancelled}. Standard signal cycles and safety clearance phases were restored."
    else:
        text = f"Incident {incident_row.get('id')}: Emergency priority green corridor successfully granted for {v_type} {v_id} towards {dest}. Safety all-red clearance and post-clearance cross-street flush phases were strictly executed."

    return IncidentSummaryResponse(
        incident_id=incident_row.get("id", "UNKNOWN"),
        vehicle_id=v_id,
        summary_text=text,
        source="keyword_fallback",
    )
