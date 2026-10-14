"""
LifeLane Emergency Backend – Routing, ETA & Corridor Plan Generator
==================================================================
Calculates Haversine distances, ETAs to junctions, determines approach roads,
and builds the dynamic green wave corridor schedule.
"""

from __future__ import annotations

import math
from typing import Any

# Earth radius in meters
EARTH_RADIUS_METERS = 6371000.0

# ── Reference Junction Network ──────────────────────────────────────────────
# Junctions with coordinates and approach road orientations
JUNCTIONS = {
    "J_CENTRAL": {
        "name": "Central Crossing (Junction C)",
        "lat": 28.6139,
        "lon": 77.2090,
        "tls_id": "C",
    },
    "J_NORTH": {
        "name": "North Radial Junction",
        "lat": 28.6190,
        "lon": 77.2090,
        "tls_id": "N_TL",
    },
    "J_SOUTH": {
        "name": "South Radial Junction",
        "lat": 28.6088,
        "lon": 77.2090,
        "tls_id": "S_TL",
    },
    "J_EAST": {
        "name": "East Radial Junction",
        "lat": 28.6139,
        "lon": 77.2150,
        "tls_id": "E_TL",
    },
    "J_WEST": {
        "name": "West Radial Junction",
        "lat": 28.6139,
        "lon": 77.2030,
        "tls_id": "W_TL",
    },
}

# Known destination coordinates
DESTINATIONS = {
    "city general hospital": {"lat": 28.6250, "lon": 28.6250, "corridor": ["J_SOUTH", "J_CENTRAL", "J_NORTH"]},
    "metro trauma center":    {"lat": 28.6040, "lon": 77.2090, "corridor": ["J_NORTH", "J_CENTRAL", "J_SOUTH"]},
    "central fire station":   {"lat": 28.6139, "lon": 77.2200, "corridor": ["J_WEST", "J_CENTRAL", "J_EAST"]},
}


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance between two GPS coordinates in meters."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_METERS * c


def compute_eta_seconds(distance_meters: float, speed_kmh: float = 50.0) -> float:
    """Calculate ETA in seconds given distance and speed."""
    speed_mps = max(speed_kmh, 5.0) * (1000.0 / 3600.0)  # convert km/h to m/s
    return round(distance_meters / speed_mps, 1)


def determine_approach_road(veh_lat: float, veh_lon: float, junc_lat: float, junc_lon: float) -> str:
    """Determine which approach arm the vehicle is arriving on (North, South, East, West)."""
    d_lat = junc_lat - veh_lat
    d_lon = junc_lon - veh_lon

    # If vehicle is south of junction (d_lat > 0), it is approaching from the South
    if abs(d_lat) >= abs(d_lon):
        return "South" if d_lat > 0 else "North"
    else:
        return "West" if d_lon > 0 else "East"


def find_route_to_destination(
    current_lat: float,
    current_lon: float,
    destination: str,
    vehicle_type: str,
) -> list[str]:
    """Find the sequence of junctions towards the destination.

    Private vehicles receive a shorter corridor (maximum 1-2 junctions).
    """
    dest_key = destination.strip().lower()

    if dest_key in DESTINATIONS:
        corridor = list(DESTINATIONS[dest_key]["corridor"])
    else:
        # Default corridor: sort all junctions by distance to current vehicle position
        sorted_juncs = sorted(
            JUNCTIONS.keys(),
            key=lambda j: haversine_distance_meters(current_lat, current_lon, JUNCTIONS[j]["lat"], JUNCTIONS[j]["lon"]),
        )
        corridor = [sorted_juncs[0], "J_CENTRAL"]
        if sorted_juncs[1] not in corridor:
            corridor.append(sorted_juncs[1])

    # Rule 3: Private vehicles get a shorter corridor (capped at max 1 or 2 nearest junctions)
    is_private = vehicle_type.startswith("private_")
    if is_private:
        corridor = corridor[:1]  # clear only the immediate critical junction for private vehicles

    return corridor


def build_corridor_plan(
    current_lat: float,
    current_lon: float,
    destination: str,
    vehicle_type: str,
    speed_kmh: float = 50.0,
) -> dict[str, Any]:
    """Build the full corridor plan:

    - Computes distance and ETA to each junction along the route.
    - Sets green_start_time = max(0, ETA - 40 seconds).
    - Sets green_duration (official emergency = 45s, private emergency = 25s).
    - Enforces shorter corridor for private vehicles.
    """
    is_private = vehicle_type.startswith("private_")
    green_duration = 25.0 if is_private else 45.0
    advance_notice_seconds = 40.0

    junction_route = find_route_to_destination(current_lat, current_lon, destination, vehicle_type)

    junction_plans = []
    prev_lat, prev_lon = current_lat, current_lon
    cumulative_distance = 0.0

    for j_id in junction_route:
        j_info = JUNCTIONS.get(j_id, JUNCTIONS["J_CENTRAL"])
        j_lat, j_lon = j_info["lat"], j_info["lon"]

        # Leg distance from current position
        dist_from_vehicle = haversine_distance_meters(current_lat, current_lon, j_lat, j_lon)
        eta_sec = compute_eta_seconds(dist_from_vehicle, speed_kmh)

        # Approach road
        approach = determine_approach_road(current_lat, current_lon, j_lat, j_lon)

        # Time to start green: ETA minus 40 seconds (Rule 3)
        green_start_sec = max(0.0, round(eta_sec - advance_notice_seconds, 1))

        junction_plans.append({
            "junction_id": j_id,
            "junction_name": j_info["name"],
            "tls_id": j_info["tls_id"],
            "approach_road": approach,
            "distance_meters": round(dist_from_vehicle, 1),
            "eta_seconds": eta_sec,
            "green_start_seconds": green_start_sec,
            "green_duration_seconds": green_duration,
            "advance_clearance_notice_seconds": advance_notice_seconds,
        })

    return {
        "vehicle_type": vehicle_type,
        "is_private": is_private,
        "corridor_length_junctions": len(junction_plans),
        "total_estimated_eta_seconds": junction_plans[-1]["eta_seconds"] if junction_plans else 0.0,
        "junction_schedules": junction_plans,
    }
