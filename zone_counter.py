#!/usr/bin/env python3
"""
LifeLane – Zone-Based Vehicle Counter
======================================
Detects, tracks, and counts vehicles in up to 4 user-defined polygon zones
using YOLOv8 + ByteTrack.  For each zone it computes vehicle count, weighted
PCU count, queue length (metres), and average waiting time.

Outputs:
  • Annotated video  → output.mp4
  • Per-snapshot JSON → counts.jsonl
  • Live REST API     → GET /counts

Usage:
    python zone_counter.py                        # run with zone_config.yaml
    python zone_counter.py --config my.yaml       # use a different config
    python zone_counter.py --pick-zones           # draw zones on first frame
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time as _time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import yaml
from ultralytics import YOLO

# ── FastAPI (runs in a daemon thread so the main loop owns the GIL) ──────────
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

api = FastAPI(title="LifeLane Zone Counter")

# Shared state: latest snapshot published by the processing loop
_latest: dict = {}
_lock = threading.Lock()


@api.get("/counts")
def get_counts():
    """Return the most recent per-zone vehicle metrics."""
    with _lock:
        return JSONResponse(content=_latest)


def _serve_api(port: int) -> None:
    """Start uvicorn in the background; daemon=True kills it on exit."""
    uvicorn.run(api, host="0.0.0.0", port=port, log_level="warning")


# ── Geometry helpers ─────────────────────────────────────────────────────────

def point_in_polygon(px: float, py: float, polygon: np.ndarray) -> bool:
    """Return True if (px, py) lies inside *polygon* (OpenCV contour)."""
    return cv2.pointPolygonTest(polygon, (float(px), float(py)), False) >= 0


def draw_zones(
    frame: np.ndarray,
    zones: list[dict],
    zone_stats: dict[str, dict] | None = None,
) -> np.ndarray:
    """Draw semi-transparent coloured polygon overlays, labels, and stats."""
    overlay = frame.copy()
    for z in zones:
        poly = z["_poly_np"]
        color = tuple(z.get("color", [0, 255, 0]))
        cv2.fillPoly(overlay, [poly], color)
        cv2.polylines(frame, [poly], isClosed=True, color=color, thickness=2)

        # Zone name at centroid
        M = cv2.moments(poly)
        if M["m00"] == 0:
            continue
        cx = int(M["m10"] / M["m00"])
        cy = int(M["m01"] / M["m00"])
        cv2.putText(
            frame, z["name"], (cx - 40, cy - 10),
            cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2,
        )

        # Live stats below the name
        if zone_stats and z["name"] in zone_stats:
            s = zone_stats[z["name"]]
            line1 = f"{s['current_count']} vehs  wt={s['weighted_count']:.1f}"
            line2 = f"Q={s['queue_length_m']:.1f}m  W={s['avg_wait_sec']:.1f}s"
            cv2.putText(
                frame, line1, (cx - 60, cy + 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1,
            )
            cv2.putText(
                frame, line2, (cx - 60, cy + 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1,
            )

    # Blend the translucent fill at 25 % opacity
    return cv2.addWeighted(overlay, 0.25, frame, 0.75, 0)


# ── Per-vehicle state tracker ────────────────────────────────────────────────

class VehicleState:
    """Keeps a sliding window of centre-point positions for every tracked ID
    so we can estimate speed and accumulate waiting time per zone."""

    MAX_HISTORY = 60  # ~2 s at 30 fps

    def __init__(self):
        # track_id → [(t, cx, cy), ...]
        self.positions: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
        # (track_id, zone_name) → timestamp of first "slow" observation
        self.wait_start: dict[tuple[int, str], float] = {}
        # zone_name → {track_id, ...}   cumulative unique vehicles seen
        self.zone_seen: dict[str, set[int]] = defaultdict(set)

    # ---- position / speed --------------------------------------------------

    def update(self, tid: int, cx: float, cy: float, t: float) -> None:
        buf = self.positions[tid]
        buf.append((t, cx, cy))
        if len(buf) > self.MAX_HISTORY:
            del buf[: len(buf) - self.MAX_HISTORY]

    def speed_px_s(self, tid: int) -> float:
        """Instantaneous speed in px/s from last two observations."""
        buf = self.positions.get(tid)
        if not buf or len(buf) < 2:
            return 0.0
        t1, x1, y1 = buf[-2]
        t2, x2, y2 = buf[-1]
        dt = t2 - t1
        if dt <= 0:
            return 0.0
        return float(np.hypot(x2 - x1, y2 - y1) / dt)

    # ---- waiting time per zone ---------------------------------------------

    def mark_waiting(self, tid: int, zone: str, t: float) -> None:
        key = (tid, zone)
        if key not in self.wait_start:
            self.wait_start[key] = t

    def clear_waiting(self, tid: int, zone: str) -> None:
        self.wait_start.pop((tid, zone), None)

    def get_wait(self, tid: int, zone: str, t: float) -> float:
        start = self.wait_start.get((tid, zone))
        return (t - start) if start is not None else 0.0

    # ---- housekeeping ------------------------------------------------------

    def cleanup(self, active: set[int]) -> None:
        """Drop tracks that have disappeared from the scene."""
        stale = set(self.positions) - active
        for tid in stale:
            del self.positions[tid]
            for k in [k for k in self.wait_start if k[0] == tid]:
                del self.wait_start[k]


# ── Interactive zone picker ──────────────────────────────────────────────────

def pick_zones(video_source) -> list[dict]:
    """Open the first frame and let the user click polygon vertices.

    Controls
    --------
    Left-click   add a vertex
    ENTER        finish current zone (needs ≥ 3 vertices)
    Q            done – save and quit
    """
    cap = cv2.VideoCapture(video_source)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        sys.exit(f"ERROR: cannot read first frame from {video_source}")

    COLORS = [(0, 255, 0), (0, 0, 255), (255, 0, 0), (0, 255, 255)]
    NAMES = ["North", "South", "East", "West"]
    zones: list[dict] = []
    pts: list[list[int]] = []
    idx = 0

    def _click(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            pts.append([x, y])

    win = "LifeLane Zone Picker  |  click → ENTER → Q"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(win, _click)

    print("\n╔══════════════════════════════════════╗")
    print("║   Zone Picker                        ║")
    print("║   Left-click = add vertex             ║")
    print("║   ENTER      = finish zone             ║")
    print("║   Q          = done                    ║")
    print("╚══════════════════════════════════════╝\n")

    while idx < 4:
        vis = frame.copy()
        # Completed zones
        for z in zones:
            cv2.polylines(vis, [np.array(z["polygon"])], True, tuple(z["color"]), 2)
            cv2.putText(vis, z["name"], tuple(z["polygon"][0]),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, tuple(z["color"]), 2)
        # Current polygon-in-progress
        col = COLORS[idx % len(COLORS)]
        for i, p in enumerate(pts):
            cv2.circle(vis, tuple(p), 5, col, -1)
            if i > 0:
                cv2.line(vis, tuple(pts[i - 1]), tuple(p), col, 2)
        if len(pts) > 2:
            cv2.line(vis, tuple(pts[-1]), tuple(pts[0]), col, 1)

        cv2.imshow(win, vis)
        key = cv2.waitKey(30) & 0xFF

        if key == 13 and len(pts) >= 3:          # ENTER
            name = NAMES[idx] if idx < len(NAMES) else f"Zone{idx}"
            zones.append({"name": name, "color": list(col), "polygon": pts.copy()})
            print(f"  ✓ Zone '{name}' saved ({len(pts)} vertices)")
            pts.clear()
            idx += 1
        elif key in (ord("q"), ord("Q")):
            if len(pts) >= 3:
                name = NAMES[idx] if idx < len(NAMES) else f"Zone{idx}"
                zones.append({"name": name, "color": list(col), "polygon": pts.copy()})
                print(f"  ✓ Zone '{name}' saved ({len(pts)} vertices)")
            break

    cv2.destroyAllWindows()
    return zones


# ── Metric computation ───────────────────────────────────────────────────────

def compute_zone_metrics(
    zone_name: str,
    vehicles: list[tuple[int, str, float, float, float]],
    state: VehicleState,
    weights: dict[str, float],
    speed_thresh_px_s: float,
    ppm: float,
    t: float,
) -> dict:
    """Compute all metrics for a single zone at time *t*.

    Parameters
    ----------
    vehicles : list of (track_id, class_label, cx, cy, bbox_height)
    """
    count = len(vehicles)
    total_unique = len(state.zone_seen.get(zone_name, set()))

    # ── class breakdown & weighted (PCU) count ──
    by_class: dict[str, int] = defaultdict(int)
    weighted = 0.0
    for _, label, *_ in vehicles:
        by_class[label] += 1
        weighted += weights.get(label, 1.0)

    # ── queue length & waiting time ──
    halted_xy: list[tuple[float, float]] = []
    wait_times: list[float] = []

    for tid, _lbl, cx, cy, _bh in vehicles:
        spd = state.speed_px_s(tid)
        if spd < speed_thresh_px_s:
            halted_xy.append((cx, cy))
            state.mark_waiting(tid, zone_name, t)
            wait_times.append(state.get_wait(tid, zone_name, t))
        else:
            state.clear_waiting(tid, zone_name)

    # Queue length = max pairwise distance among halted centres → metres
    if len(halted_xy) >= 2:
        pts = np.array(halted_xy)
        # For ≤ ~100 vehicles the brute-force pairwise is fine
        diffs = pts[:, None, :] - pts[None, :, :]
        queue_px = float(np.sqrt((diffs ** 2).sum(axis=-1)).max())
    else:
        queue_px = 0.0
    queue_m = round(queue_px / max(ppm, 0.01), 1)

    avg_wait = round(sum(wait_times) / len(wait_times), 1) if wait_times else 0.0

    return {
        "current_count": count,
        "total_unique": total_unique,
        "weighted_count": round(weighted, 2),
        "queue_length_m": queue_m,
        "avg_wait_sec": avg_wait,
        "halted_count": len(halted_xy),
        "by_class": dict(by_class),
    }


# ── Main loop ────────────────────────────────────────────────────────────────

def run(cfg_path: str, pick: bool = False) -> None:
    # ────── load config ──────
    cfg_text = Path(cfg_path).read_text(encoding="utf-8")
    cfg: dict = yaml.safe_load(cfg_text)

    video_src = cfg["video_source"]
    # Convert "0" (string) to int for webcam
    if isinstance(video_src, str) and video_src.strip().isdigit():
        video_src = int(video_src.strip())

    # ────── interactive zone picker (optional) ──────
    if pick:
        picked = pick_zones(video_src)
        if not picked:
            sys.exit("No zones defined – exiting.")
        cfg["zones"] = picked
        Path(cfg_path).write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False),
                                  encoding="utf-8")
        print(f"  → Zones written back to {cfg_path}\n")

    zones: list[dict] = cfg.get("zones", [])
    if not zones:
        sys.exit("ERROR: no zones defined.  Run with --pick-zones or edit the config file.")
    if len(zones) > 4:
        print("WARNING: only the first 4 zones will be used.")
        zones = zones[:4]

    # Pre-compute numpy contour arrays for cv2.pointPolygonTest
    for z in zones:
        z["_poly_np"] = np.array(z["polygon"], dtype=np.int32)

    # ────── read remaining config values with sensible defaults ──────
    class_map: dict[int, str] = {
        int(k): v for k, v in cfg.get("class_map", {
            2: "car", 3: "motorcycle", 5: "bus", 7: "truck",
        }).items()
    }
    weights: dict[str, float] = cfg.get("weights", {
        "motorcycle": 0.5,
        "car": 1.0,
        "auto_rickshaw": 0.75,
        "bus": 2.5,
        "truck": 2.5,
    })
    ppm           = float(cfg.get("pixels_per_metre", 15.0))
    speed_thr_kmh = float(cfg.get("speed_threshold_kmh", 5.0))
    speed_thr_pxs = (speed_thr_kmh / 3.6) * ppm        # → pixels / second
    interval      = float(cfg.get("report_interval_sec", 2.0))
    conf          = float(cfg.get("confidence", 0.3))
    model_name    = cfg.get("model", "yolov8n.pt")
    out_vid       = cfg.get("output_video", "output.mp4")
    out_jsonl     = cfg.get("output_jsonl", "counts.jsonl")
    api_port      = int(cfg.get("api_port", 8001))

    # ────── start the FastAPI server in a background daemon thread ──────
    threading.Thread(target=_serve_api, args=(api_port,), daemon=True).start()
    print(f"  [API]   http://localhost:{api_port}/counts")

    # ────── load YOLO ──────
    print(f"  [MODEL] Loading {model_name} …")
    model = YOLO(model_name)

    # ────── open video ──────
    cap = cv2.VideoCapture(video_src)
    if not cap.isOpened():
        sys.exit(f"ERROR: cannot open video source '{video_src}'")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0                                   # webcam fallback
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    total_str = str(total) if total > 0 else "∞"

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_vid, fourcc, fps, (W, H))
    print(f"  [VIDEO] {W}×{H} @ {fps:.1f} fps  ({total_str} frames) → {out_vid}")

    # ────── state bookkeeping ──────
    state = VehicleState()
    jsonl_fh = open(out_jsonl, "w", encoding="utf-8")  # noqa: SIM115
    frame_no = 0
    last_report = 0.0
    latest_stats: dict[str, dict] = {}

    print(f"  [RUN]   Processing … press Q in the video window to stop.\n")
    print(f"  {'t':>7s} │ {'Zone':^8s} │ {'Now':>3s}  {'Tot':>4s}  "
          f"{'PCU':>5s}  {'Queue':>6s}  {'Wait':>5s}")
    print(f"  {'─' * 7}─┼─{'─' * 8}─┼─{'─' * 3}──{'─' * 4}──"
          f"{'─' * 5}──{'─' * 6}──{'─' * 5}")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t = frame_no / fps
            frame_no += 1

            # ── detect + track ──
            results = model.track(
                frame,
                persist=True,                        # keep IDs across frames
                tracker="bytetrack.yaml",            # shipped with ultralytics
                conf=conf,
                classes=list(class_map.keys()),
                verbose=False,
            )

            # ── parse boxes ──
            active_ids: set[int] = set()
            # zone_name → [(track_id, label, cx, cy, bbox_h)]
            zone_vehs: dict[str, list] = {z["name"]: [] for z in zones}

            boxes = results[0].boxes if results else None
            has_tracks = boxes is not None and boxes.id is not None

            if has_tracks:
                for i in range(len(boxes)):
                    cls_id = int(boxes.cls[i])
                    if cls_id not in class_map:
                        continue

                    tid = int(boxes.id[i])
                    x1, y1, x2, y2 = boxes.xyxy[i].tolist()
                    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                    bh = y2 - y1
                    label = class_map[cls_id]

                    active_ids.add(tid)
                    state.update(tid, cx, cy, t)

                    # Draw bounding box + label
                    cv2.rectangle(frame, (int(x1), int(y1)),
                                  (int(x2), int(y2)), (180, 180, 180), 1)
                    tag = f"{label} #{tid}"
                    cv2.putText(frame, tag, (int(x1), int(y1) - 4),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.40,
                                (255, 255, 255), 1, cv2.LINE_AA)

                    # Assign vehicle to every zone it falls in
                    for z in zones:
                        if point_in_polygon(cx, cy, z["_poly_np"]):
                            zone_vehs[z["name"]].append((tid, label, cx, cy, bh))
                            state.zone_seen[z["name"]].add(tid)

            # ── periodic metric snapshot ──
            if t - last_report >= interval and t > 0:
                last_report = t
                report: dict[str, dict] = {}

                for z in zones:
                    zn = z["name"]
                    m = compute_zone_metrics(
                        zn, zone_vehs[zn], state, weights,
                        speed_thr_pxs, ppm, t,
                    )
                    report[zn] = m
                    print(
                        f"  {t:6.1f}s │ {zn:^8s} │ {m['current_count']:3d}  "
                        f"{m['total_unique']:4d}  {m['weighted_count']:5.1f}  "
                        f"{m['queue_length_m']:5.1f}m  {m['avg_wait_sec']:4.1f}s"
                    )

                latest_stats = report

                # Write JSONL record
                record = {"timestamp_sec": round(t, 2), "zones": report}
                jsonl_fh.write(json.dumps(record) + "\n")
                jsonl_fh.flush()

                # Publish to FastAPI
                with _lock:
                    _latest.update(record)

            # ── draw zone overlays and HUD ──
            frame = draw_zones(frame, zones, latest_stats)
            cv2.putText(frame, f"t={t:.1f}s  frame={frame_no}",
                        (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 255, 255), 2, cv2.LINE_AA)

            writer.write(frame)

            # Live preview window (scale down if very large)
            scale = min(1.0, 1280 / W, 720 / H)
            if scale < 1.0:
                preview = cv2.resize(frame, None, fx=scale, fy=scale)
            else:
                preview = frame
            cv2.imshow("LifeLane Zone Counter", preview)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

            # Garbage-collect stale tracks every 10 seconds
            if frame_no % int(fps * 10) == 0:
                state.cleanup(active_ids)

    except KeyboardInterrupt:
        print("\n  [STOP] Interrupted.")

    finally:
        cap.release()
        writer.release()
        jsonl_fh.close()
        cv2.destroyAllWindows()
        print(f"\n  [DONE] Video  → {out_vid}")
        print(f"  [DONE] Counts → {out_jsonl}")
        print(f"  [DONE] API was at http://localhost:{api_port}/counts")


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="LifeLane – zone-based vehicle counter with YOLO + ByteTrack",
    )
    ap.add_argument(
        "--config", default="zone_config.yaml",
        help="Path to YAML config file  (default: zone_config.yaml)",
    )
    ap.add_argument(
        "--pick-zones", action="store_true",
        help="Interactively draw polygon zones on the first video frame",
    )
    args = ap.parse_args()
    run(args.config, args.pick_zones)


if __name__ == "__main__":
    main()
