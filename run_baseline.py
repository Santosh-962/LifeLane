#!/usr/bin/env python3
"""
LifeLane – Baseline Fixed-Timer Simulation Runner
=================================================
Runs the 4-way 2-lane junction simulation using SUMO & TraCI with a fixed-timer
traffic light program (40s Green, 3s Yellow, 2s All-Red).

Logs per-second telemetry to CSV:
  - Average waiting time (seconds)
  - Queue length (vehicle count & meters) per approach arm: North, South, East, West
  - Cumulative arrived vehicles and total/average travel time

Outputs:
  - results_baseline.csv
  - Formatted performance summary printed to console

Usage:
  python run_baseline.py              # runs headless (sumo)
  python run_baseline.py --gui        # runs with visual sumo-gui
  python run_baseline.py --time 1800  # runs for 1800 seconds (30 mins)
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import time
from pathlib import Path

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

# ── Approach Arms and Inbound Edges ──────────────────────────────────────────
APPROACH_EDGES = {
    "North": "N2C",
    "South": "S2C",
    "East":  "E2C",
    "West":  "W2C",
}


def get_arm_queue_metrics(edge_id: str) -> tuple[int, float]:
    """Calculate the number of halted vehicles and physical queue length in meters

    for a given inbound edge.
    Halted vehicle: speed < 0.1 m/s (as defined by SUMO).
    Queue length (m): distance from stop line (end of edge) to the furthest halted vehicle.
    """
    lane_ids = [f"{edge_id}_0", f"{edge_id}_1"]
    halted_count = 0
    furthest_halted_dist = 0.0

    edge_len = traci.lane.getLength(lane_ids[0])

    for lane in lane_ids:
        vehs = traci.lane.getLastStepVehicleIDs(lane)
        for v in vehs:
            if traci.vehicle.getSpeed(v) < 0.1:  # halted
                halted_count += 1
                pos = traci.vehicle.getLanePosition(v)
                dist_to_stopline = edge_len - pos
                if dist_to_stopline > furthest_halted_dist:
                    furthest_halted_dist = dist_to_stopline

    return halted_count, round(furthest_halted_dist, 1)


def find_sumo_binary(use_gui: bool) -> str:
    """Find appropriate sumo or sumo-gui binary on system."""
    binary_name = "sumo-gui" if use_gui else "sumo"

    # 1. Check system PATH
    found = shutil.which(binary_name)
    if found:
        return found

    # 2. Check SUMO_HOME/bin
    if "SUMO_HOME" in os.environ:
        candidate = os.path.join(os.environ["SUMO_HOME"], "bin", f"{binary_name}.exe")
        if os.path.exists(candidate):
            return candidate
        candidate_posix = os.path.join(os.environ["SUMO_HOME"], "bin", binary_name)
        if os.path.exists(candidate_posix):
            return candidate_posix

    # 3. Fallback to binary name and let OS raise clear error
    return binary_name


def run_simulation(
    sumocfg_path: str,
    output_csv: str,
    sim_duration: int = 1000,
    use_gui: bool = False,
    tls_id: str = "C",
) -> None:
    sumo_bin = find_sumo_binary(use_gui)
    print(f"\n[LifeLane] Starting Baseline Simulation")
    print(f"  Binary:      {sumo_bin}")
    print(f"  Config:      {sumocfg_path}")
    print(f"  Duration:    {sim_duration} steps (seconds)")
    print(f"  Logging to:  {output_csv}\n")

    sumo_cmd = [
        sumo_bin,
        "-c", sumocfg_path,
        "--step-length", "1",
        "--no-step-log", "true",
        "--waiting-time-memory", "3600",
    ]

    # Start TraCI
    try:
        traci.start(sumo_cmd)
    except Exception as e:
        sys.exit(f"Failed to start SUMO via TraCI:\n{e}")

    # Track vehicle departure & travel times
    depart_times: dict[str, float] = {}
    completed_travel_times: list[float] = []

    # Queue tracking for final statistics
    peak_queues: dict[str, int] = {arm: 0 for arm in APPROACH_EDGES}
    sum_queues: dict[str, int] = {arm: 0 for arm in APPROACH_EDGES}

    # Open CSV writer
    csv_file = open(output_csv, mode="w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow([
        "sim_time_sec",
        "tls_phase_index",
        "queue_north_veh",
        "queue_north_m",
        "queue_south_veh",
        "queue_south_m",
        "queue_east_veh",
        "queue_east_m",
        "queue_west_veh",
        "queue_west_m",
        "active_vehicle_count",
        "cumulative_arrived_vehicles",
        "avg_waiting_time_active_sec",
        "avg_travel_time_completed_sec",
        "total_travel_time_sec",
    ])

    step = 0
    t_start = time.time()

    print(f"{'Time':>6s} │ {'Phase':>5s} │ {'North (Q)':>10s} │ {'South (Q)':>10s} │ {'East (Q)':>10s} │ {'West (Q)':>10s} │ {'Actv':>5s} │ {'AvgWait':>8s}")
    print("─" * 6 + "─┼─" + "─" * 5 + "─┼─" + "─" * 10 + "─┼─" + "─" * 10 + "─┼─" + "─" * 10 + "─┼─" + "─" * 10 + "─┼─" + "─" * 5 + "─┼─" + "─" * 8)

    try:
        while step < sim_duration and traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            sim_time = traci.simulation.getTime()

            # Record departures of newly entered vehicles
            for veh_id in traci.simulation.getDepartedIDList():
                depart_times[veh_id] = sim_time

            # Record arrival and completed trip travel time
            for veh_id in traci.simulation.getArrivedIDList():
                if veh_id in depart_times:
                    trip_duration = sim_time - depart_times[veh_id]
                    completed_travel_times.append(trip_duration)

            # Traffic signal phase
            curr_phase = traci.trafficlight.getPhase(tls_id)

            # Active vehicles & waiting time
            active_ids = traci.vehicle.getIDList()
            active_count = len(active_ids)
            total_active_waiting = sum(traci.vehicle.getWaitingTime(v) for v in active_ids)
            avg_active_waiting = (total_active_waiting / active_count) if active_count > 0 else 0.0

            # Queue metrics per approach arm
            arm_metrics = {}
            for arm, edge in APPROACH_EDGES.items():
                cnt, dist = get_arm_queue_metrics(edge)
                arm_metrics[arm] = (cnt, dist)
                sum_queues[arm] += cnt
                if cnt > peak_queues[arm]:
                    peak_queues[arm] = cnt

            # Travel time stats
            arrived_count = len(completed_travel_times)
            tot_travel_time = sum(completed_travel_times)
            avg_travel_time = (tot_travel_time / arrived_count) if arrived_count > 0 else 0.0

            # Write row to CSV
            writer.writerow([
                int(sim_time),
                curr_phase,
                arm_metrics["North"][0], arm_metrics["North"][1],
                arm_metrics["South"][0], arm_metrics["South"][1],
                arm_metrics["East"][0],  arm_metrics["East"][1],
                arm_metrics["West"][0],  arm_metrics["West"][1],
                active_count,
                arrived_count,
                round(avg_active_waiting, 2),
                round(avg_travel_time, 2),
                round(tot_travel_time, 2),
            ])

            # Periodic console display (every 10s)
            if step % 10 == 0:
                print(
                    f"{int(sim_time):5d}s │ {curr_phase:5d} │ "
                    f"{arm_metrics['North'][0]:3d} ({arm_metrics['North'][1]:4.0f}m) │ "
                    f"{arm_metrics['South'][0]:3d} ({arm_metrics['South'][1]:4.0f}m) │ "
                    f"{arm_metrics['East'][0]:3d} ({arm_metrics['East'][1]:4.0f}m) │ "
                    f"{arm_metrics['West'][0]:3d} ({arm_metrics['West'][1]:4.0f}m) │ "
                    f"{active_count:5d} │ {avg_active_waiting:7.1f}s"
                )

            step += 1

    except KeyboardInterrupt:
        print("\n[LifeLane] Simulation interrupted by user.")
    finally:
        csv_file.close()
        traci.close()

    elapsed_wallclock = time.time() - t_start

    # Summary report
    print("\n" + "=" * 58)
    print("           BASELINE SIMULATION RESULTS SUMMARY           ")
    print("=" * 58)
    print(f"Total simulated time:    {step} seconds")
    print(f"Wall-clock run time:     {elapsed_wallclock:.2f} seconds")
    print(f"Total vehicles arrived:  {len(completed_travel_times)}")
    print(f"Average Trip Travel Time:{avg_travel_time:.2f} s")
    print(f"Total Trip Travel Time:  {tot_travel_time:.1f} s")
    print("─" * 58)
    print("Approach Arm Queue Breakdown:")
    for arm in APPROACH_EDGES:
        avg_q = sum_queues[arm] / max(step, 1)
        print(f"  {arm:6s} Arm -> Peak Queue: {peak_queues[arm]:2d} vehs | Avg Queue: {avg_q:4.1f} vehs")
    print("─" * 58)
    print(f"Detailed timeline logged to: {Path(output_csv).resolve()}\n")


def main():
    parser = argparse.ArgumentParser(description="LifeLane SUMO Baseline Simulation Runner")
    parser.add_argument(
        "--config",
        default=os.path.join("sumo_config", "cross.sumocfg"),
        help="Path to .sumocfg file (default: sumo_config/cross.sumocfg)",
    )
    parser.add_argument(
        "--output",
        default="results_baseline.csv",
        help="Output CSV path (default: results_baseline.csv)",
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
        help="Run with visual sumo-gui (default: false / headless sumo)",
    )
    args = parser.parse_args()

    run_simulation(
        sumocfg_path=args.config,
        output_csv=args.output,
        sim_duration=args.time,
        use_gui=args.gui,
    )


if __name__ == "__main__":
    main()
