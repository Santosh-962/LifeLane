"""
LifeLane Emergency Backend – Emergency Request Verification Rules
=================================================================
Enforces safety and authorization policies:
  1. Official vehicles (ambulance, fire, police) must be active and registered
     in the 'vehicles' database.
  2. Private vehicles are accepted ONLY for medical ('private_medical') or fire
     ('private_fire') emergencies, AND require a valid 112 Reference ID or
     hospital name.
  3. Any other request or unauthorized vehicle is rejected with an explicit reason.
"""

from __future__ import annotations

import sqlite3
from typing import Tuple

OFFICIAL_VEHICLE_TYPES = {"ambulance", "fire", "police"}
PRIVATE_EMERGENCY_TYPES = {"private_medical", "private_fire"}
ALL_VALID_TYPES = OFFICIAL_VEHICLE_TYPES | PRIVATE_EMERGENCY_TYPES


def verify_emergency_request(
    db_conn: sqlite3.Connection,
    vehicle_id: str,
    vehicle_type: str,
    reference_id: str | None,
) -> Tuple[bool, str]:
    """Verify an emergency vehicle preemption request.

    Returns:
        (is_accepted: bool, explanation_or_rejection_reason: str)
    """
    v_type = vehicle_type.strip().lower() if vehicle_type else ""
    v_id = vehicle_id.strip() if vehicle_id else ""
    ref_id = reference_id.strip() if reference_id else ""

    # Rule 0: Vehicle ID must be provided
    if not v_id:
        return False, "Rejection: 'vehicle_id' cannot be blank."

    # Rule 1: Validate allowed vehicle categories
    if v_type not in ALL_VALID_TYPES:
        if v_type.startswith("private_"):
            return (
                False,
                f"Rejection: Private vehicles can raise alerts ONLY for medical or fire emergencies. Received '{v_type}'.",
            )
        return (
            False,
            f"Rejection: Unknown vehicle type '{v_type}'. Allowed types are: {', '.join(sorted(ALL_VALID_TYPES))}.",
        )

    # Rule 2: Official emergency vehicles (Ambulance, Fire, Police)
    if v_type in OFFICIAL_VEHICLE_TYPES:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT vehicle_id, vehicle_type, department, plate_number, status FROM vehicles WHERE vehicle_id = ?",
            (v_id,),
        )
        row = cur.fetchone()
        if not row:
            return (
                False,
                f"Rejection: Official {v_type} '{v_id}' not found in registered emergency vehicle registry.",
            )

        reg_type = row["vehicle_type"].lower()
        reg_status = row["status"].lower()
        department = row["department"]
        plate = row["plate_number"]

        # Ensure vehicle type matches registered class
        if reg_type != v_type:
            return (
                False,
                f"Rejection: Registered vehicle '{v_id}' is a '{reg_type}', but request claimed '{v_type}'.",
            )

        # Check vehicle active status
        if reg_status not in ("active", "registered"):
            return (
                False,
                f"Rejection: Vehicle '{v_id}' status is '{reg_status}' (not authorized for active emergency duty).",
            )

        return (
            True,
            f"Verified: Registered {v_type} #{v_id} ({department}, Plate: {plate}). Priority corridor approved.",
        )

    # Rule 3: Private vehicles (private_medical or private_fire)
    if v_type in PRIVATE_EMERGENCY_TYPES:
        if not ref_id:
            emergency_kind = "medical" if v_type == "private_medical" else "fire"
            return (
                False,
                f"Rejection: Private {emergency_kind} alert requires a verified 112 Reference ID or Hospital Name.",
            )

        emergency_label = "Medical" if v_type == "private_medical" else "Fire"
        return (
            True,
            f"Verified: Private {emergency_label} emergency approved with verification reference '{ref_id}'. Shorter safety corridor allocated.",
        )

    return False, "Rejection: Emergency authorization criteria not met."
