#!/usr/bin/env python3
"""
LifeLane – Adaptive Traffic Signal Controller & Camera Bridge
============================================================
Dynamically adjusts green times based on traffic queues and weighted demand.

Supports two data sources:
  1. --source sumo    : Reads live vehicle counts and queue lengths directly from
                        SUMO simulation edges via TraCI.
  2. --source camera  : Fetches real-time perception data from the YOLO vision service
                        via GET /counts (from zone_counter.py).

Fault Tolerance & Safety Rules:
  - Yellow (3s) and All-Red safety clearance (2s) are NEVER skipped or shortened.
  - If camera data is missing, disconnected, or delayed (> stale-timeout), the
    controller automatically falls back to the safe fixed-timer plan (40s Green).
  - Automatically resumes adaptive mode once fresh camera data arrives.

Outputs:
  - results_adaptive.csv
  - Live console telemetry dashboard showing data source, mode, and allocated green times.

Usage:
  python adaptive_controller.py --source sumo --gui
  python adaptive_controller.py --source camera --camera-url http://localhost:8001/counts
  python adaptive_controller.py --source camera --gui --output results_camera_adaptive.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── Ensure SUMO_HOME is set and tools can be imported ───────────────────────
if "SUMO_HOME" in os.environ:
    tools_path = os.path.join(os.environ["SUMO_HOME"], "tools")
    if tools_path not in sys.path:
        sys.path.append(tools_path)

try:
    import traci
except ImportError:
    sys.exit(
        "ERROR: 'traci' module not found.\n"
        "Ensure SUMO is installed and SUMO_HOME is configured, or install via:\n"
        "  pip install traci sumolib"
    )

# ── Approaches & Signal Phases ──────────────────────────────────────────────
APPROACH_EDGES = {
    "North": "N2C",
    "South": "S2C",
    "East":  "E2C",
    "West":  "W2C",
}

# Phase indices matching cross.net.xml / cross.tll.xml:
# 0: NS Green, 1: NS Yellow, 2: All-Red, 3: EW Green, 4: EW Yellow, 5: All-Red
PHASE_NS_GREEN = 0
PHASE_NS_YELLOW = 1
PHASE_ALL_RED_1 = 2
PHASE_EW_GREEN = 3
PHASE_EW_YELLOW = 4
PHASE_ALL_RED_2 = 5

YELLOW_DURATION = 3
ALL_RED_DURATION = 2


class CameraDataBridge:
    """Fetches and validates per-zone vehicle metrics from the vision API."""

    def __init__(self, endpoint_url: str, stale_timeout: float = 4.0):
        self.endpoint_url = endpoint_url
        self.stale_timeout = stale_timeout
        self.last_valid_data: dict[str, Any] | None = None
        self.last_success_wallclock: float = 0.0
        self.last_camera_timestamp: float = -1.0
        self.is_healthy: bool = False
        self.health_message: str = "Initializing camera bridge..."

    def fetch(self) -> tuple[dict[str, Any] | None, bool, str]:
        """Fetch latest counts.

        Returns (data, is_valid, status_msg).
        """
        now = time.time()
        try:
            req = urllib.request.Request(
                self.endpoint_url,
                headers={"User-Agent": "LifeLane-SignalBridge/1.0"},
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status != 200:
                    self.is_healthy = False
                    self.health_message = f"HTTP {resp.status} from camera API"
                    return None, False, self.health_message

                raw = resp.read().decode("utf-8")
                data = json.loads(raw)

        except urllib.error.URLError as e:
            self.is_healthy = False
            self.health_message = f"Connection error: {e.reason}"
            return None, False, self.health_message
        except Exception as e:
            self.is_healthy = False
            self.health_message = f"Failed to parse payload: {e}"
            return None, False, self.health_message

        # Check payload structure
        zones = data.get("zones")
        if not isinstance(zones, dict) or not zones:
            self.is_healthy = False
            self.health_message = "Empty or invalid 'zones' payload from camera"
            return None, False, self.health_message

        cam_ts = float(data.get("timestamp_sec", 0.0))

        # Check if camera frame timestamp is advancing or stuck
        if self.last_valid_data is not None:
            time_since_fetch = now - self.last_success_wallclock
            if time_since_fetch > self.stale_timeout:
                self.is_healthy = False
                self.health_message = f"Data stale: no fresh frame for {time_since_fetch:.1f}s"
                return None, False, self.health_message

        self.last_valid_data = data
        self.last_success_wallclock = now
        self.last_camera_timestamp = cam_ts
        self.is_healthy = True
        self.health_message = f"Healthy (t={cam_ts:.1f}s)"
        return data, True, self.health_message


def get_sumo_traffic_demand() -> dict[str, dict[str, float]]:
    """Extract live traffic demand per approach directly from SUMO."""
    data = {}
    for arm, edge in APPROACH_EDGES.items():
        lane_0 = f"{edge}_0"
        lane_1 = f"{edge}_1"

        halted = traci.edge.getLastStepHaltingNumber(edge)
        total_vehs = traci.edge.getLastStepVehicleNumber(edge)

        # Measure physical queue distance to furthest stopped vehicle
        edge_len = traci.lane.getLength(lane_0)
        furthest_q = 0.0
        for lane in (lane_0, lane_1):
            for v in traci.lane.getLastStepVehicleIDs(lane):
                if traci.vehicle.getSpeed(v) < 0.1:
                    dist = edge_len - traci.vehicle.getLanePosition(v)
                    if dist > furthest_q:
                        furthest_q = dist

        data[arm] = {
            "halted_count": halted,
            "vehicle_count": total_vehs,
            "weighted_count": float(total_vehs),  # in SUMO 1 veh ≈ 1 PCU
            "queue_length_m": round(furthest_q, 1),
        }
    return data


def compute_adaptive_green(
    demand_active_corridor: float,
    demand_competing_corridor: float,
    min_green: int = 15,
    max_green: int = 60,
    base_green: int = 25,
) -> int:
    """Proportional queue-weighted green allocation algorithm.

    Allocates dynamic green time bounded by [min_green, max_green]:
      ratio = active / (active + competing + epsilon)
      green = min_green + (max_green - min_green) * ratio
    """
    total = demand_active_corridor + demand_competing_corridor
    if total <= 0:
        return base_green

    ratio = demand_active_corridor / total
    allocated = min_green + int(round((max_green - min_green) * ratio))
    return max(min_green, min(max_green, allocated))


def find_sumo_binary(use_gui: bool) -> str:
    binary_name = "sumo-gui" if use_gui else "sumo"
    found = shutil.which(binary_name)
    if found:
        return found
    if "SUMO_HOME" in os.environ:
        for ext in (".exe", ""):
            cand = os.path.join(os.environ["SUMO_HOME"], "bin", f"{binary_name}{ext}")
            if os.path.exists(cand):
                return cand
    return binary_name


def run_adaptive_simulation(
    sumocfg_path: str,
    output_csv: str,
    source: str = "camera",
    camera_url: str = "http://localhost:8001/counts",
    stale_timeout: float = 4.0,
    sim_duration: int = 1000,
    use_gui: bool = False,
    min_green: int = 15,
    max_green: int = 60,
    fixed_fallback_green: int = 40,
    tls_id: str = "C",
) -> None:
    sumo_bin = find_sumo_binary(use_gui)

    print("\n" + "=" * 65)
    print("   LIFELANE ADAPTIVE TRAFFIC SIGNAL CONTROLLER & BRIDGE   ")
    print("=" * 65)
    print(f"  Data Source:     {source.upper()}")
    if source == "camera":
        print(f"  Camera API URL:  {camera_url}")
        print(f"  Stale Timeout:   {stale_timeout}s (falls back to fixed plan)")
    print(f"  Green Limits:    Min={min_green}s | Max={max_green}s | Fallback Fixed={fixed_fallback_green}s")
    print(f"  Safety Clear:    Yellow={YELLOW_DURATION}s | All-Red={ALL_RED_DURATION}s (MANDATORY)")
    print(f"  SUMO Binary:     {sumo_bin}")
    print(f"  Duration:        {sim_duration} seconds")
    print(f"  Output CSV:      {output_csv}")
    print("=" * 65 + "\n")

    camera_bridge = CameraDataBridge(camera_url, stale_timeout) if source == "camera" else None

    # Start SUMO via TraCI
    sumo_cmd = [
        sumo_bin,
        "-c", sumocfg_path,
        "--step-length", "1",
        "--no-step-log", "true",
        "--waiting-time-memory", "3600",
    ]

    try:
        traci.start(sumo_cmd)
    except Exception as e:
        sys.exit(f"Failed to start SUMO via TraCI:\n{e}")

    # CSV Logging setup
    csv_file = open(output_csv, mode="w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow([
        "sim_time_sec",
        "tls_phase_index",
        "phase_name",
        "control_mode",          # 'ADAPTIVE_CAM', 'ADAPTIVE_SUMO', 'FALLBACK_FIXED'
        "current_green_budget_s",
        "phase_time_elapsed_s",
        "demand_north",
        "demand_south",
        "demand_east",
        "demand_west",
        "active_vehicle_count",
        "cumulative_arrived_vehicles",
        "avg_waiting_time_active_s",
        "avg_travel_time_completed_s",
    ])

    depart_times: dict[str, float] = {}
    completed_travel_times: list[float] = []

    # Controller State Machine
    current_phase = PHASE_NS_GREEN
    phase_timer = 0
    current_green_budget = fixed_fallback_green
    control_mode = "ADAPTIVE_SUMO" if source == "sumo" else "INITIALIZING"

    # Set initial phase in SUMO
    traci.trafficlight.setPhase(tls_id, current_phase)

    step = 0
    t_start = time.time()

    print(f"{'Time':>6s} │ {'Phase':^11s} │ {'Mode':^14s} │ {'Budget':>6s} │ {'Elaps':>5s} │ {'N / S Dem':>11s} │ {'E / W Dem':>11s} │ {'AvgWait':>7s}")
    print("─" * 6 + "─┼─" + "─" * 11 + "─┼─" + "─" * 14 + "─┼─" + "─" * 6 + "─┼─" + "─" * 5 + "─┼─" + "─" * 11 + "─┼─" + "─" * 11 + "─┼─" + "─" * 7)

    try:
        while step < sim_duration and traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            sim_time = traci.simulation.getTime()

            # Track departures and arrivals for true travel-time measurement
            for vid in traci.simulation.getDepartedIDList():
                depart_times[vid] = sim_time
            for vid in traci.simulation.getArrivedIDList():
                if vid in depart_times:
                    completed_travel_times.append(sim_time - depart_times[vid])

            # ── 1. Acquire Traffic Demand from Source ───────────────────────
            demands = {"North": 0.0, "South": 0.0, "East": 0.0, "West": 0.0}
            fallback_active = False
            fallback_reason = ""

            if source == "camera":
                assert camera_bridge is not None
                cam_data, is_valid, msg = camera_bridge.fetch()
                if is_valid and cam_data:
                    control_mode = "ADAPTIVE_CAM"
                    zones = cam_data.get("zones", {})
                    for arm in demands:
                        z_stat = zones.get(arm, {})
                        # Use weighted_count (PCUs) or halted queue count
                        demands[arm] = float(z_stat.get("weighted_count", z_stat.get("current_count", 0.0)))
                else:
                    control_mode = "FALLBACK_FIXED"
                    fallback_active = True
                    fallback_reason = msg
            else:
                control_mode = "ADAPTIVE_SUMO"
                sumo_dem = get_sumo_traffic_demand()
                for arm in demands:
                    demands[arm] = sumo_dem[arm]["weighted_count"]

            demand_NS = demands["North"] + demands["South"]
            demand_EW = demands["East"] + demands["West"]

            # ── 2. Signal Phase State Machine with Strict All-Red Safety ───
            phase_timer += 1

            if current_phase == PHASE_NS_GREEN:
                # Check if NS green budget reached
                if phase_timer >= current_green_budget:
                    # Transition to NS Yellow (3s) -> Never skip safety
                    current_phase = PHASE_NS_YELLOW
                    phase_timer = 0
                    traci.trafficlight.setPhase(tls_id, current_phase)

            elif current_phase == PHASE_NS_YELLOW:
                if phase_timer >= YELLOW_DURATION:
                    # Transition to mandatory All-Red clearance (2s)
                    current_phase = PHASE_ALL_RED_1
                    phase_timer = 0
                    traci.trafficlight.setPhase(tls_id, current_phase)

            elif current_phase == PHASE_ALL_RED_1:
                if phase_timer >= ALL_RED_DURATION:
                    # Clearance complete -> switch to EW Green
                    current_phase = PHASE_EW_GREEN
                    phase_timer = 0

                    if fallback_active:
                        current_green_budget = fixed_fallback_green
                    else:
                        current_green_budget = compute_adaptive_green(
                            demand_active_corridor=demand_EW,
                            demand_competing_corridor=demand_NS,
                            min_green=min_green,
                            max_green=max_green,
                            base_green=fixed_fallback_green,
                        )
                    traci.trafficlight.setPhase(tls_id, current_phase)

            elif current_phase == PHASE_EW_GREEN:
                if phase_timer >= current_green_budget:
                    # Transition to EW Yellow (3s)
                    current_phase = PHASE_EW_YELLOW
                    phase_timer = 0
                    traci.trafficlight.setPhase(tls_id, current_phase)

            elif current_phase == PHASE_EW_YELLOW:
                if phase_timer >= YELLOW_DURATION:
                    # Transition to mandatory All-Red clearance (2s)
                    current_phase = PHASE_ALL_RED_2
                    phase_timer = 0
                    traci.trafficlight.setPhase(tls_id, current_phase)

            elif current_phase == PHASE_ALL_RED_2:
                if phase_timer >= ALL_RED_DURATION:
                    # Clearance complete -> switch to NS Green
                    current_phase = PHASE_NS_GREEN
                    phase_timer = 0

                    if fallback_active:
                        current_green_budget = fixed_fallback_green
                    else:
                        current_green_budget = compute_adaptive_green(
                            demand_active_corridor=demand_NS,
                            demand_competing_corridor=demand_EW,
                            min_green=min_green,
                            max_green=max_green,
                            base_green=fixed_fallback_green,
                        )
                    traci.trafficlight.setPhase(tls_id, current_phase)

            # ── 3. Performance Metrics Calculation ──────────────────────────
            active_ids = traci.vehicle.getIDList()
            active_count = len(active_ids)
            total_active_waiting = sum(traci.vehicle.getWaitingTime(v) for v in active_ids)
            avg_active_waiting = (total_active_waiting / active_count) if active_count > 0 else 0.0

            arrived_count = len(completed_travel_times)
            tot_travel_time = sum(completed_travel_times)
            avg_travel_time = (tot_travel_time / arrived_count) if arrived_count > 0 else 0.0

            phase_names = {
                PHASE_NS_GREEN:  "NS GREEN",
                PHASE_NS_YELLOW: "NS YELLOW",
                PHASE_ALL_RED_1: "ALL-RED",
                PHASE_EW_GREEN:  "EW GREEN",
                PHASE_EW_YELLOW: "EW YELLOW",
                PHASE_ALL_RED_2: "ALL-RED",
            }
            p_name = phase_names.get(current_phase, f"Phase {current_phase}")

            # Write to CSV
            writer.writerow([
                int(sim_time),
                current_phase,
                p_name,
                control_mode,
                current_green_budget,
                phase_timer,
                round(demands["North"], 1),
                round(demands["South"], 1),
                round(demands["East"], 1),
                round(demands["West"], 1),
                active_count,
                arrived_count,
                round(avg_active_waiting, 2),
                round(avg_travel_time, 2),
            ])

            # Live CLI output every 5s or upon phase switch
            if step % 5 == 0 or phase_timer == 1:
                dem_ns_str = f"{demands['North']:3.0f} / {demands['South']:3.0f}"
                dem_ew_str = f"{demands['East']:3.0f} / {demands['West']:3.0f}"
                alert = f" [!] {fallback_reason[:20]}" if fallback_active else ""
                print(
                    f"{int(sim_time):5d}s │ {p_name:^11s} │ {control_mode:^14s} │ "
                    f"{current_green_budget:4d}s │ {phase_timer:3d}s  │ "
                    f"{dem_ns_str:>11s} │ {dem_ew_str:>11s} │ {avg_active_waiting:6.1f}s{alert}"
                )

            step += 1

    except KeyboardInterrupt:
        print("\n[LifeLane] Controller stopped by user.")
    finally:
        csv_file.close()
        traci.close()

    elapsed = time.time() - t_start

    print("\n" + "=" * 65)
    print("            ADAPTIVE CONTROLLER RESULTS SUMMARY           ")
    print("=" * 65)
    print(f"Total simulated time:    {step} seconds")
    print(f"Wall-clock run time:     {elapsed:.2f} seconds")
    print(f"Completed trips arrived: {len(completed_travel_times)}")
    print(f"Average Trip Travel Time:{avg_travel_time:.2f} s")
    print(f"Total Trip Travel Time:  {tot_travel_time:.1f} s")
    print(f"Output CSV Logged to:    {Path(output_csv).resolve()}")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="LifeLane Adaptive Signal Controller with Camera/SUMO Bridge",
    )
    parser.add_argument(
        "--source",
        choices=["sumo", "camera"],
        default="camera",
        help="Queue detection source: 'camera' (YOLO GET /counts) or 'sumo' (TraCI internal)",
    )
    parser.add_argument(
        "--camera-url",
        default="http://localhost:8001/counts",
        help="URL of the camera counts endpoint (default: http://localhost:8001/counts)",
    )
    parser.add_argument(
        "--stale-timeout",
        type=float,
        default=4.0,
        help="Seconds of missing/delayed camera data before falling back to fixed plan (default: 4.0)",
    )
    parser.add_argument(
        "--config",
        default=os.path.join("sumo_config", "cross.sumocfg"),
        help="Path to .sumocfg file (default: sumo_config/cross.sumocfg)",
    )
    parser.add_argument(
        "--output",
        default="results_adaptive.csv",
        help="Output CSV path (default: results_adaptive.csv)",
    )
    parser.add_argument(
        "--time",
        type=int,
        default=1000,
        help="Simulation duration in seconds (default: 1000)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch with visual sumo-gui (default: headless sumo)",
    )
    parser.add_argument(
        "--min-green",
        type=int,
        default=15,
        help="Minimum adaptive green time in seconds (default: 15)",
    )
    parser.add_argument(
        "--max-green",
        type=int,
        default=60,
        help="Maximum adaptive green time in seconds (default: 60)",
    )
    parser.add_argument(
        "--fixed-fallback-green",
        type=int,
        default=40,
        help="Fixed green time in seconds during fallback mode (default: 40)",
    )
    args = parser.parse_args()

    run_adaptive_simulation(
        sumocfg_path=args.config,
        output_csv=args.output,
        source=args.source,
        camera_url=args.camera_url,
        stale_timeout=args.stale_timeout,
        sim_duration=args.time,
        use_gui=args.gui,
        min_green=args.min_green,
        max_green=args.max_green,
        fixed_fallback_green=args.fixed_fallback_green,
    )


if __name__ == "__main__":
    main()
