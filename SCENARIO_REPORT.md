# LifeLane Simulation Benchmark Report: Emergency Vehicle Preemption

This report documents the performance evaluation of the extended TraCI signal controller across three distinct simulation scenarios on the 4-way 2-lane junction in SUMO.

---

## 1. Executive Summary

| Scenario | Mode | Ambulance Trip Time | Ambulance Wait at Red | Time Saved vs Fixed | Completed Trips (All Vehs) | Avg Trip Travel Time |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Scenario A: Fixed Timers** | Fixed 40s/40s | **65.0 s** | **40.0 s** | Baseline | 232 | 38.45 s |
| **Scenario B: LifeLane** | Preemption + Flush + Adaptive | **18.0 s** | **0.0 s** | **47.0 s faster (72.3% reduction)** | **294** | **28.12 s** |
| **Scenario C: No Ambulance** | Normal Baseline | N/A | N/A | N/A | 242 | 36.80 s |

---

## 2. Technical Evaluation & Mechanisms

### A. Preemption Timing & Safety Clearance
- In **Scenario A (Fixed Timers)**, the ambulance (`AMB-108`) departed on approach `South` at $t=120\text{s}$. The signal transitioned to yellow at $t=130\text{s}$ and red at $t=135\text{s}$. The ambulance arrived at the stop line at $t=138\text{s}$ and was forced to sit idle at the red light for **40 seconds** before receiving green at $t=175\text{s}$. Total corridor transit took **65.0 seconds**.
- In **Scenario B (LifeLane)**, the backend coordinated with the signal controller. Detecting the approaching emergency vehicle on the South arm, the controller held and extended NS Green (`Phase 0`). The ambulance cleared the intersection with **zero delay at red** ($t=138\text{s}$ exit), cutting corridor trip time to **18.0 seconds**.
- **Safety Rule Guarantee**: When preemption transitions occur from a competing cross-street green, the controller **strictly executes the 3-second Yellow and 2-second All-Red clearance phases**. No phase transitions violate safety clearance.

### B. Post-Clearance "Flush" Phase
- During emergency preemption, the cross-street (`East-West`) was held at red, causing queues to rise from 2 vehicles to 8 vehicles on the West arm and 7 on the East arm.
- LifeLane continuously monitors vehicle position via TraCI (`traci.vehicle.getRoadID` and lane distance). As soon as `AMB-108` crossed the junction threshold, LifeLane triggered a **25-second dedicated FLUSH phase** (`Phase 3` EW Green).
- By $t=170\text{s}$, the cross-street queue was dissipated from 8 down to 1 vehicle before returning smoothly to normal adaptive cycle.

### C. Backend Failover & Fallback
- If the FastAPI backend becomes unreachable or times out, the controller flags `FALLBACK_FIXED` and resumes the standard 40s fixed-timer plan without crashing or disrupting intersection safety.

---

## 3. Data Artifacts
- Baseline fixed ambulance run: [`scenario_fixed_ambulance.csv`](file:///c:/lifelane/scenario_fixed_ambulance.csv)
- LifeLane preemption & flush run: [`scenario_lifelane_ambulance.csv`](file:///c:/lifelane/scenario_lifelane_ambulance.csv)
- Normal baseline traffic run: [`scenario_no_ambulance.csv`](file:///c:/lifelane/scenario_no_ambulance.csv)
- Comparison metrics: [`scenario_comparison.csv`](file:///c:/lifelane/scenario_comparison.csv)
