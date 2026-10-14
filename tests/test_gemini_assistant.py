"""
Unit Tests for LifeLane Gemini Assistant & Keyword Fallbacks
===========================================================
Tests natural language parsing, triage classification, facility alert generation,
police incident summary generation, and schema validation.
"""

import unittest
from emergency_backend.gemini_assistant import (
    EmergencyDescriptionResponse,
    FacilityAlertResponse,
    IncidentSummaryResponse,
    generate_facility_alert,
    generate_police_incident_summary,
    parse_emergency_text,
)


class TestGeminiAssistantAndFallbacks(unittest.TestCase):
    """Test Gemini natural language perception and deterministic fallback rules."""

    # ── 1. Emergency Free Text Parsing (POST /emergency/describe) ────────────

    def test_car_fire_driver_injured_parses_as_fire_medical(self):
        """'car fire near the hospital, driver injured' must identify fire + injury,

        recommend both fire_engine and ambulance, and extract location.
        """
        text = "car fire near the hospital, driver injured"
        res = parse_emergency_text(text)

        self.assertIsInstance(res, EmergencyDescriptionResponse)
        self.assertEqual(res.emergency_type, "FIRE_MEDICAL")
        self.assertEqual(res.severity, "CRITICAL")
        self.assertIn("fire_engine", res.units_needed)
        self.assertIn("ambulance", res.units_needed)
        self.assertIn("hospital", res.location_text.lower())

    def test_pure_medical_cardiac_arrest_parses_critical(self):
        """Cardiac arrest call must be classified as MEDICAL with CRITICAL severity."""
        text = "Elderly patient having cardiac arrest at Central Market"
        res = parse_emergency_text(text)

        self.assertIsInstance(res, EmergencyDescriptionResponse)
        self.assertEqual(res.emergency_type, "MEDICAL")
        self.assertEqual(res.severity, "CRITICAL")
        self.assertIn("ambulance", res.units_needed)
        self.assertIn("central market", res.location_text.lower())

    def test_pure_fire_blaze(self):
        """Warehouse fire must be classified as FIRE with fire_engine."""
        text = "Massive warehouse blaze and heavy smoke on South Radial Road"
        res = parse_emergency_text(text)

        self.assertIsInstance(res, EmergencyDescriptionResponse)
        self.assertEqual(res.emergency_type, "FIRE")
        self.assertIn("fire_engine", res.units_needed)
        self.assertIn("south radial road", res.location_text.lower())

    def test_traffic_accident_parsing(self):
        """Overturned vehicle crash without fire/casualty keywords classifies as TRAFFIC_ACCIDENT."""
        text = "Major vehicle collision and overturned truck near Metro Trauma Center"
        res = parse_emergency_text(text)

        self.assertIsInstance(res, EmergencyDescriptionResponse)
        self.assertIn(res.emergency_type, ("TRAFFIC_ACCIDENT", "MEDICAL"))
        self.assertIn("ambulance", res.units_needed)

    def test_empty_string_handles_gracefully(self):
        """Empty input must not crash and must return a valid validated Pydantic model."""
        res = parse_emergency_text("")
        self.assertIsInstance(res, EmergencyDescriptionResponse)
        self.assertEqual(res.emergency_type, "OTHER")
        self.assertIn(res.source, ("gemini", "keyword_fallback"))

    # ── 2. Facility Alert Message Generator ─────────────────────────────────

    def test_facility_alert_message_formatting(self):
        """Hospital operational alert must contain vehicle ID, ETA, and patient profile."""
        res = generate_facility_alert(
            vehicle_id="AMB-108",
            emergency_type="FIRE_MEDICAL",
            patient_type="Driver with severe smoke inhalation and third-degree burns",
            destination="City General Hospital",
            eta_seconds=45,
        )

        self.assertIsInstance(res, FacilityAlertResponse)
        self.assertEqual(res.destination, "City General Hospital")
        self.assertEqual(res.eta_seconds, 45)
        self.assertIn("AMB-108", res.alert_message)
        self.assertIn("45", res.alert_message)
        self.assertIn("City General Hospital", res.alert_message)

    # ── 3. Police Incident Summary Generator ────────────────────────────────

    def test_police_incident_summary_for_successful_preemption(self):
        """Police summary must accurately report vehicle ID, corridor status, and clearance."""
        incident_data = {
            "id": "EMG-A1B2C3D4",
            "vehicle_id": "AMB-108",
            "vehicle_type": "ambulance",
            "destination": "City General Hospital",
            "status": "COMPLETED",
            "created_at": "2026-10-14T12:00:00Z",
            "rejection_reason": None,
            "cancelled_by": None,
        }

        res = generate_police_incident_summary(incident_data)
        self.assertIsInstance(res, IncidentSummaryResponse)
        self.assertEqual(res.incident_id, "EMG-A1B2C3D4")
        self.assertEqual(res.vehicle_id, "AMB-108")
        self.assertIn("AMB-108", res.summary_text)
        self.assertIn("City General Hospital", res.summary_text)
        self.assertTrue(len(res.summary_text) > 20)

    def test_police_incident_summary_for_rejected_request(self):
        """Rejected requests must note the rejection reason for audit review."""
        incident_data = {
            "id": "EMG-REJ-9988",
            "vehicle_id": "PVT-FAKE-01",
            "vehicle_type": "private_medical",
            "destination": "Metro Trauma Center",
            "status": "REJECTED",
            "created_at": "2026-10-14T12:05:00Z",
            "rejection_reason": "Missing 112 Reference ID",
            "cancelled_by": None,
        }

        res = generate_police_incident_summary(incident_data)
        self.assertIsInstance(res, IncidentSummaryResponse)
        self.assertIn("REJECTED", res.summary_text)
        self.assertIn("Missing 112 Reference ID", res.summary_text)


if __name__ == "__main__":
    unittest.main()
