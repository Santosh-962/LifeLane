"""
Unit Tests for LifeLane Emergency Backend
========================================
Tests verification rules, ETA calculation, corridor scheduling,
and police overrides.
"""

import math
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from emergency_backend.corridor import (
    build_corridor_plan,
    compute_eta_seconds,
    determine_approach_road,
    haversine_distance_meters,
)
from emergency_backend.db import (
    get_db,
    init_db,
    log_police_override,
    save_emergency_request,
    set_emergency_status,
)
from emergency_backend.verifier import verify_emergency_request


class TestEmergencyVerificationRules(unittest.TestCase):
    """Test official vehicle registry checks and private vehicle verification rules."""

    def setUp(self):
        # Create a fresh temporary in-memory/file SQLite database for each test
        self.temp_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_file.close()
        self.db_path = Path(self.temp_db_file.name)
        init_db(self.db_path)
        self.conn = get_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # ── Official Vehicles ───────────────────────────────────────────────────

    def test_registered_ambulance_is_accepted(self):
        """Registered ambulance (e.g. AMB-108) must be accepted."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="AMB-108",
            vehicle_type="ambulance",
            reference_id=None,
        )
        self.assertTrue(ok)
        self.assertIn("Verified", msg)

    def test_unregistered_ambulance_is_rejected(self):
        """Unregistered ambulance ID must be rejected."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="AMB-FAKE-999",
            vehicle_type="ambulance",
            reference_id=None,
        )
        self.assertFalse(ok)
        self.assertIn("not found in registered emergency vehicle registry", msg)

    def test_registered_fire_is_accepted(self):
        """Registered fire brigade vehicle must be accepted."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="FIRE-101",
            vehicle_type="fire",
            reference_id=None,
        )
        self.assertTrue(ok)
        self.assertIn("Verified", msg)

    def test_unregistered_fire_is_rejected(self):
        """Unregistered fire truck must be rejected."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="FIRE-UNKNOWN",
            vehicle_type="fire",
            reference_id=None,
        )
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_mismatched_vehicle_type_is_rejected(self):
        """If a vehicle is registered as fire, claiming it is an ambulance must fail."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="FIRE-101",
            vehicle_type="ambulance",
            reference_id=None,
        )
        self.assertFalse(ok)
        self.assertIn("claimed 'ambulance'", msg)

    # ── Private Vehicles ────────────────────────────────────────────────────

    def test_private_medical_with_reference_id_is_accepted(self):
        """Private medical vehicle with 112 ID or hospital name must be accepted."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="PVT-CAR-789",
            vehicle_type="private_medical",
            reference_id="112-DEL-2024-9988",
        )
        self.assertTrue(ok)
        self.assertIn("Verified", msg)
        self.assertIn("112-DEL-2024-9988", msg)

    def test_private_medical_without_reference_id_is_rejected(self):
        """Private medical vehicle missing reference ID must be rejected."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="PVT-CAR-789",
            vehicle_type="private_medical",
            reference_id=None,
        )
        self.assertFalse(ok)
        self.assertIn("requires a verified 112 Reference ID or Hospital Name", msg)

        # Also test with empty string or whitespace
        ok_empty, _ = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="PVT-CAR-789",
            vehicle_type="private_medical",
            reference_id="   ",
        )
        self.assertFalse(ok_empty)

    def test_private_fire_with_reference_id_is_accepted(self):
        """Private fire emergency with reference ID must be accepted."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="PVT-TRK-442",
            vehicle_type="private_fire",
            reference_id="FIRE-CALL-7712",
        )
        self.assertTrue(ok)
        self.assertIn("Verified", msg)

    def test_private_fire_without_reference_id_is_rejected(self):
        """Private fire vehicle missing reference ID must be rejected."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="PVT-TRK-442",
            vehicle_type="private_fire",
            reference_id=None,
        )
        self.assertFalse(ok)
        self.assertIn("requires a verified 112 Reference ID", msg)

    def test_private_general_non_emergency_is_rejected(self):
        """Private vehicles claiming non-medical/fire reasons must be rejected."""
        ok, msg = verify_emergency_request(
            db_conn=self.conn,
            vehicle_id="PVT-CAR-111",
            vehicle_type="private_commute",
            reference_id="LATE_FOR_WORK",
        )
        self.assertFalse(ok)
        self.assertIn("ONLY for medical or fire emergencies", msg)


class TestCorridorAndETACalculations(unittest.TestCase):
    """Test Haversine distance, ETA computation, approach detection, and green scheduling."""

    def test_haversine_distance(self):
        """Verify Haversine distance between two known coordinates."""
        # 0.01 deg latitude ≈ 1113 meters
        dist = haversine_distance_meters(28.6100, 77.2090, 28.6200, 77.2090)
        self.assertAlmostEqual(dist, 1112.0, delta=20.0)

    def test_compute_eta(self):
        """At 50 km/h (13.89 m/s), 1000m should yield ~72 seconds ETA."""
        eta = compute_eta_seconds(distance_meters=1000.0, speed_kmh=50.0)
        expected = round(1000.0 / (50.0 * 1000.0 / 3600.0), 1)
        self.assertEqual(eta, expected)
        self.assertAlmostEqual(eta, 72.0, delta=1.0)

    def test_determine_approach_road(self):
        """Vehicle positioned south of junction must be classified as approaching from South."""
        # Junction at (28.6139, 77.2090), vehicle at (28.6000, 77.2090) -> South approach
        approach = determine_approach_road(
            veh_lat=28.6000, veh_lon=77.2090,
            junc_lat=28.6139, junc_lon=77.2090,
        )
        self.assertEqual(approach, "South")

        # Vehicle north of junction -> North approach
        approach_north = determine_approach_road(
            veh_lat=28.6250, veh_lon=77.2090,
            junc_lat=28.6139, junc_lon=77.2090,
        )
        self.assertEqual(approach_north, "North")

    def test_corridor_plan_green_timing_rule(self):
        """Green start time must be exactly max(0, ETA - 40 seconds)."""
        plan = build_corridor_plan(
            current_lat=28.6000,
            current_lon=77.2090,
            destination="City General Hospital",
            vehicle_type="ambulance",
            speed_kmh=50.0,
        )
        schedules = plan["junction_schedules"]
        self.assertGreater(len(schedules), 0)

        for sched in schedules:
            eta = sched["eta_seconds"]
            expected_start = max(0.0, round(eta - 40.0, 1))
            self.assertEqual(sched["green_start_seconds"], expected_start)

    def test_private_vehicles_receive_shorter_corridor(self):
        """Private vehicles must receive a shorter corridor (duration & junctions) compared to official emergency."""
        official_plan = build_corridor_plan(
            current_lat=28.6000,
            current_lon=77.2090,
            destination="City General Hospital",
            vehicle_type="ambulance",
            speed_kmh=50.0,
        )
        private_plan = build_corridor_plan(
            current_lat=28.6000,
            current_lon=77.2090,
            destination="City General Hospital",
            vehicle_type="private_medical",
            speed_kmh=50.0,
        )

        # Private corridor has shorter green duration
        self.assertLess(
            private_plan["junction_schedules"][0]["green_duration_seconds"],
            official_plan["junction_schedules"][0]["green_duration_seconds"],
        )
        self.assertEqual(private_plan["junction_schedules"][0]["green_duration_seconds"], 25.0)
        self.assertEqual(official_plan["junction_schedules"][0]["green_duration_seconds"], 45.0)

        # Private corridor has fewer junctions
        self.assertLessEqual(
            private_plan["corridor_length_junctions"],
            official_plan["corridor_length_junctions"],
        )


class TestDatabaseAndOverrides(unittest.TestCase):
    """Test persistence, cancellation, and police overrides."""

    def setUp(self):
        self.temp_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_file.close()
        self.db_path = Path(self.temp_db_file.name)
        init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_save_and_cancel_emergency(self):
        """Ensure requests can be saved and cancelled by the driver."""
        save_emergency_request(
            request_id="EMG-TEST-01",
            vehicle_id="AMB-108",
            vehicle_type="ambulance",
            status="ACTIVE",
            lat=28.6100,
            lon=77.2090,
            destination="City General Hospital",
            reference_id=None,
            plan={"test": 123},
            db_path=self.db_path,
        )

        conn = get_db(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT status FROM emergency_log WHERE id = 'EMG-TEST-01'")
        self.assertEqual(cur.fetchone()["status"], "ACTIVE")
        conn.close()

        # Cancel request
        modified = set_emergency_status("EMG-TEST-01", "CANCELLED", cancelled_by="driver", db_path=self.db_path)
        self.assertTrue(modified)

        conn = get_db(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT status, cancelled_by FROM emergency_log WHERE id = 'EMG-TEST-01'")
        row = cur.fetchone()
        self.assertEqual(row["status"], "CANCELLED")
        self.assertEqual(row["cancelled_by"], "driver")
        conn.close()

    def test_police_override_logging(self):
        """Ensure police override actions are properly audited."""
        override_id = log_police_override(
            officer_id="OFFICER-77",
            action="cancel_corridor",
            target_id="EMG-TEST-01",
            reason="Corridor cleared ahead of schedule",
            db_path=self.db_path,
        )
        self.assertGreater(override_id, 0)

        conn = get_db(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT officer_id, action FROM police_override_log WHERE id = ?", (override_id,))
        row = cur.fetchone()
        self.assertEqual(row["officer_id"], "OFFICER-77")
        self.assertEqual(row["action"], "cancel_corridor")
        conn.close()


if __name__ == "__main__":
    unittest.main()
