"""
LifeLane Emergency Backend – Database & Models (SQLite)
======================================================
Manages SQLite persistence for registered vehicles, emergency corridor requests,
and police override audit trails.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path("lifelane_emergency.db")


def get_db(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Connect to SQLite database with Row factory."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    """Initialize database tables and seed sample registered emergency vehicles."""
    conn = get_db(db_path)
    cur = conn.cursor()

    # 1. Official emergency vehicles registry
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            vehicle_id TEXT PRIMARY KEY,
            vehicle_type TEXT NOT NULL,
            department TEXT NOT NULL,
            plate_number TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
    """)

    # 2. Emergency requests & corridor execution log
    cur.execute("""
        CREATE TABLE IF NOT EXISTS emergency_log (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            vehicle_id TEXT NOT NULL,
            vehicle_type TEXT NOT NULL,
            status TEXT NOT NULL, -- 'ACTIVE', 'CANCELLED', 'COMPLETED', 'REJECTED', 'POLICE_OVERRIDDEN'
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            speed_kmh REAL NOT NULL DEFAULT 50.0,
            destination TEXT NOT NULL,
            reference_id TEXT,
            plan_json TEXT,
            rejection_reason TEXT,
            cancelled_by TEXT
        )
    """)

    # 3. Police override audit log
    cur.execute("""
        CREATE TABLE IF NOT EXISTS police_override_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            officer_id TEXT NOT NULL,
            action TEXT NOT NULL,
            target_emergency_id TEXT,
            reason TEXT
        )
    """)

    # Pre-seed official registered vehicles if empty
    cur.execute("SELECT COUNT(*) FROM vehicles")
    if cur.fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        sample_vehicles = [
            ("AMB-108", "ambulance", "City Emergency Medical Services", "DL-01-AM-1080", "active", now),
            ("AMB-911", "ambulance", "Metro Trauma Care Response", "DL-01-AM-0911", "active", now),
            ("FIRE-101", "fire", "Central Fire & Rescue Brigade", "DL-02-FB-1010", "active", now),
            ("FIRE-102", "fire", "North Sector Fire Unit", "DL-02-FB-1020", "active", now),
            ("POLICE-99", "police", "Traffic Police Emergency Escort", "DL-03-TP-0099", "active", now),
        ]
        cur.executemany(
            "INSERT INTO vehicles (vehicle_id, vehicle_type, department, plate_number, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            sample_vehicles,
        )

    conn.commit()
    conn.close()


def save_emergency_request(
    request_id: str,
    vehicle_id: str,
    vehicle_type: str,
    status: str,
    lat: float,
    lon: float,
    destination: str,
    reference_id: str | None,
    plan: dict[str, Any] | None,
    rejection_reason: str | None = None,
    db_path: Path | str = DB_PATH,
) -> None:
    """Save initial emergency request to SQLite."""
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    plan_str = json.dumps(plan) if plan else None

    conn.execute(
        """
        INSERT INTO emergency_log
        (id, created_at, updated_at, vehicle_id, vehicle_type, status, lat, lon, speed_kmh, destination, reference_id, plan_json, rejection_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 50.0, ?, ?, ?, ?)
        """,
        (request_id, now, now, vehicle_id, vehicle_type, status, lat, lon, destination, reference_id, plan_str, rejection_reason),
    )
    conn.commit()
    conn.close()


def update_emergency_location_and_plan(
    request_id: str,
    lat: float,
    lon: float,
    speed_kmh: float,
    updated_plan: dict[str, Any],
    db_path: Path | str = DB_PATH,
) -> bool:
    """Update live location and refreshed corridor schedule."""
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE emergency_log
        SET lat = ?, lon = ?, speed_kmh = ?, plan_json = ?, updated_at = ?
        WHERE id = ? AND status = 'ACTIVE'
        """,
        (lat, lon, speed_kmh, json.dumps(updated_plan), now, request_id),
    )
    modified = cur.rowcount > 0
    conn.commit()
    conn.close()
    return modified


def set_emergency_status(
    request_id: str,
    new_status: str,
    cancelled_by: str | None = None,
    db_path: Path | str = DB_PATH,
) -> bool:
    """Update emergency request status (e.g. CANCELLED or POLICE_OVERRIDDEN)."""
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE emergency_log
        SET status = ?, cancelled_by = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_status, cancelled_by, now, request_id),
    )
    modified = cur.rowcount > 0
    conn.commit()
    conn.close()
    return modified


def log_police_override(
    officer_id: str,
    action: str,
    target_id: str | None,
    reason: str | None,
    db_path: Path | str = DB_PATH,
) -> int:
    """Log police override action."""
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO police_override_log (timestamp, officer_id, action, target_emergency_id, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (now, officer_id, action, target_id, reason),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id
