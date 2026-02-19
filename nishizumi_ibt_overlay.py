"""Nishizumi IBT Overlay

A single-file Tkinter overlay that compares live iRacing telemetry to a reference lap
extracted from a local IBT file. Requires:
- Python 3 on Windows
- pyirsdk (`pip install irsdk`)
- iRacing running with telemetry enabled
- A local IBT file containing a clean reference lap

Run: python nishizumi_ibt_overlay.py
"""

from __future__ import annotations

import bisect
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import irsdk

try:
    import winsound
except ImportError:  # pragma: no cover - Windows only
    winsound = None


APP_TITLE = "Nishizumi IBT"
DEFAULT_UPDATE_MS = 16  # 60 FPS target
DEFAULT_APPROACH_A_S = 2.0
DEFAULT_APPROACH_B_S = 1.0
DEFAULT_FINAL_CUE_OFFSET_M = 0.0
DEFAULT_APPROACH_SPEED_MPS = 25.0
DEFAULT_BRAKE_THRESHOLD = 0.10
DEFAULT_LIFT_THRESHOLD = 0.20
DEFAULT_POWER_THRESHOLD = 0.70
DEFAULT_ALPHA = 0.85
DEFAULT_OVERLAY_SIZE = (900, 240)
DEFAULT_FLOW_WINDOW_S = 10.0
DEFAULT_LOOKAHEAD_WINDOW_S = 2.0
DEFAULT_FLOW_LINE_WIDTH = 2
DEFAULT_REF_LINE_WIDTH = 1.5
# Professional color scheme - vibrant with glow support
DEFAULT_LIVE_THROTTLE_COLOR = "#00ff00"  # Bright green
DEFAULT_LIVE_THROTTLE_GLOW = "#00ff00"
DEFAULT_LIVE_BRAKE_COLOR = "#ff2222"  # Bright red
DEFAULT_LIVE_BRAKE_GLOW = "#ff0000"
DEFAULT_LIVE_SPEED_COLOR = "#ffffff"
DEFAULT_REF_THROTTLE_COLOR = "#228822"  # Darker green for reference
DEFAULT_REF_BRAKE_COLOR = "#882222"  # Darker red for reference
DEFAULT_REF_SPEED_COLOR = "#6d6d6d"
# Fill colors (semi-transparent effect simulated)
DEFAULT_THROTTLE_FILL_COLOR = "#0a3a0a"  # Dark green fill
DEFAULT_BRAKE_FILL_COLOR = "#3a0a0a"  # Dark red fill
# Grid colors
DEFAULT_GRID_COLOR = "#1a1a1a"
DEFAULT_GRID_ACCENT_COLOR = "#2a2a2a"
DEFAULT_LOOKAHEAD_DISTANCE_M = 200.0
DEFAULT_LOOKAHEAD_MIN_M = 120.0
DEFAULT_LOOKAHEAD_MAX_M = 320.0
DEFAULT_LOOKAHEAD_HEIGHT = 120
DEFAULT_LOOKAHEAD_SAMPLES = 48
DEFAULT_OVERLAY_WIDTH = DEFAULT_OVERLAY_SIZE[0]
DEFAULT_OVERLAY_HEIGHT = DEFAULT_OVERLAY_SIZE[1]
ASSUMED_TRACK_LEN_M = 5000.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class TelemetrySnapshot:
    connected: bool = False
    timestamp: float = 0.0
    lap_pct: Optional[float] = None
    throttle: Optional[float] = None
    brake: Optional[float] = None
    steering: Optional[float] = None
    gear: Optional[int] = None
    speed_mps: Optional[float] = None
    track_length_km: Optional[float] = None
    lap: Optional[int] = None
    session_time: Optional[float] = None
    track_name: Optional[str] = None


@dataclass
class RefEvent:
    kind: str  # "brake", "lift", "power"
    lap_pct: float
    dist_m: Optional[float] = None


class ReferenceLap:
    def __init__(
        self,
        path: str,
        brake_threshold: float,
        lift_threshold: float,
        power_threshold: float,
    ) -> None:
        self.path = path
        self.brake_threshold = brake_threshold
        self.lift_threshold = lift_threshold
        self.power_threshold = power_threshold
        self.lap_pct: List[float] = []
        self.throttle: List[float] = []
        self.brake: List[float] = []
        self.steering: List[float] = []
        self.gear: List[int] = []
        self.speed: List[float] = []
        self.events: List[RefEvent] = []
        self.brake_points: List[float] = []
        self.track_length_m: Optional[float] = None
        self._load_ibt()

    def _load_ibt(self) -> None:
        ibt = irsdk.IBT()
        ibt.open(self.path)
        try:
            lap_pct_all = ibt.get_all("LapDistPct")
            lap_dist_all = ibt.get_all("LapDist")
            throttle_all = ibt.get_all("Throttle")
            brake_all = ibt.get_all("Brake")
            steering_all = ibt.get_all("SteeringWheelAngle")
            gear_all = ibt.get_all("Gear")
            speed_all = ibt.get_all("Speed")
        finally:
            ibt.close()

        if not lap_pct_all:
            raise ValueError("IBT file does not contain LapDistPct")

        segments = []
        start = 0
        for i in range(1, len(lap_pct_all)):
            if lap_pct_all[i] < lap_pct_all[i - 1] - 0.5:
                segments.append((start, i))
                start = i
        segments.append((start, len(lap_pct_all)))

        best_segment = max(segments, key=lambda seg: seg[1] - seg[0])
        s, e = best_segment

        self.lap_pct = lap_pct_all[s:e]
        if self.lap_pct and max(self.lap_pct) > 1.5:
            self.lap_pct = [pct / 100.0 for pct in self.lap_pct]
        length = e - s
        self.throttle = throttle_all[s:e] if throttle_all else [0.0] * length
        self.brake = brake_all[s:e] if brake_all else [0.0] * length
        self.steering = steering_all[s:e] if steering_all else [0.0] * length
        self.gear = gear_all[s:e] if gear_all else [0] * length
        self.speed = speed_all[s:e] if speed_all else [0.0] * length

        if lap_dist_all:
            segment_dist = lap_dist_all[s:e]
            if segment_dist:
                max_dist = max(segment_dist)
                if max_dist > 0:
                    self.track_length_m = max_dist

        self._build_events()

    def _build_events(self) -> None:
        self.events = []
        self.brake_points = []
        if not self.lap_pct:
            return

        in_brake = False
        in_lift = False

        for i in range(1, len(self.lap_pct)):
            brake_val = self.brake[i]
            throttle_val = self.throttle[i]
            prev_brake = self.brake[i - 1]
            prev_throttle = self.throttle[i - 1]

            if not in_brake and prev_brake < self.brake_threshold <= brake_val:
                self.events.append(RefEvent("brake", self.lap_pct[i]))
                self.brake_points.append(self.lap_pct[i])
                in_brake = True
                in_lift = False

            if in_brake and brake_val < self.brake_threshold * 0.5:
                in_brake = False

            if (
                not in_brake
                and not in_lift
                and prev_throttle >= self.lift_threshold
                and throttle_val < self.lift_threshold
                and brake_val < self.brake_threshold
            ):
                self.events.append(RefEvent("lift", self.lap_pct[i]))
                in_lift = True

            if in_lift and throttle_val > self.lift_threshold * 1.2:
                in_lift = False

            if (in_brake or in_lift) and prev_throttle < self.power_threshold <= throttle_val:
                self.events.append(RefEvent("power", self.lap_pct[i]))

        self.brake_points.sort()

    def refresh_thresholds(
        self, brake_threshold: float, lift_threshold: float, power_threshold: float
    ) -> None:
        self.brake_threshold = brake_threshold
        self.lift_threshold = lift_threshold
        self.power_threshold = power_threshold
        self._build_events()

    def ref_at_pct(self, data: List[float], pct: float) -> float:
        if not self.lap_pct:
            return 0.0
        idx = bisect.bisect_left(self.lap_pct, pct)
        if idx <= 0:
            return data[0]
        if idx >= len(self.lap_pct):
            return data[-1]
        before = self.lap_pct[idx - 1]
        after = self.lap_pct[idx]
        if after == before:
            return data[idx]
        ratio = (pct - before) / (after - before)
        return data[idx - 1] + (data[idx] - data[idx - 1]) * ratio

    def ref_gear_at_pct(self, pct: float) -> int:
        return int(round(self.ref_at_pct(self.gear, pct)))

    def set_event_distances(self, track_len_m: float) -> None:
        for event in self.events:
            event.dist_m = event.lap_pct * track_len_m


class TelemetryWorker(threading.Thread):
    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(daemon=True)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._snapshot = TelemetrySnapshot()
        self._logger = logger

    def run(self) -> None:
        ir = irsdk.IRSDK()
        while not self._stop_event.is_set():
            try:
                if not ir.is_initialized or not ir.is_connected:
                    ir.startup()
                    if not ir.is_initialized or not ir.is_connected:
                        self._set_snapshot(TelemetrySnapshot(connected=False, timestamp=time.time()))
                        time.sleep(0.5)
                        continue

                track_length_km = self._safe_read(ir, "TrackLength")
                track_name = self._safe_session_info(ir, "WeekendInfo", "TrackName")
                if track_length_km is None:
                    track_length_km = self._parse_track_length_km(
                        self._safe_session_info(ir, "WeekendInfo", "TrackLength")
                    )

                snapshot = TelemetrySnapshot(
                    connected=True,
                    timestamp=time.time(),
                    lap_pct=self._safe_read(ir, "LapDistPct"),
                    throttle=self._safe_read(ir, "Throttle"),
                    brake=self._safe_read(ir, "Brake"),
                    steering=self._safe_read(ir, "SteeringWheelAngle"),
                    gear=self._safe_read(ir, "Gear"),
                    speed_mps=self._safe_read(ir, "Speed"),
                    track_length_km=track_length_km,
                    lap=self._safe_read(ir, "Lap"),
                    session_time=self._safe_read(ir, "SessionTime"),
                    track_name=track_name,
                )
                self._set_snapshot(snapshot)
                # High frequency polling for smooth overlays
                time.sleep(0.008)
            except Exception as exc:
                self._logger.warning("Telemetry worker error: %s", exc)
                self._set_snapshot(TelemetrySnapshot(connected=False, timestamp=time.time()))
                time.sleep(0.5)

    def _safe_read(self, ir: irsdk.IRSDK, key: str):
        try:
            return ir[key]
        except Exception:
            return None

    def _safe_session_info(self, ir: irsdk.IRSDK, *path: str) -> Optional[Any]:
        try:
            info = ir["SessionInfo"]
            for key in path:
                if not isinstance(info, dict):
                    return None
                info = info.get(key)
            return info
        except Exception:
            return None

    def _parse_track_length_km(self, value: Optional[str]) -> Optional[float]:
        if not value or not isinstance(value, str):
            return None
        text = value.strip().lower()
        try:
            if "km" in text:
                return float(text.replace("km", "").strip())
            if "mi" in text:
                miles = float(text.replace("mi", "").strip())
                return miles * 1.60934
            return float(text)
        except ValueError:
            return None

    def _set_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def get_snapshot(self) -> TelemetrySnapshot:
        with self._lock:
            return self._snapshot

    def stop(self) -> None:
        self._stop_event.set()


class AudioCues:
    # Priority values (lower = higher priority, will preempt)
    _PRIORITY_C = 0  # Final cue - highest priority
    _PRIORITY_B = 1  # Approach B
    _PRIORITY_A = 2  # Approach A - lowest priority

    def __init__(self, root: tk.Tk, logger: logging.Logger) -> None:
        self._root = root
        self._logger = logger
        self._stage_state: Dict[int, Dict[str, bool]] = {}
        self._last_dist_to_event: Dict[int, float] = {}
        self._last_lap_pct: Optional[float] = None
        self._lap_id = 0
        # Dedicated audio thread with queue for responsive playback
        self._audio_queue: queue.Queue[Optional[Tuple[int, List[Tuple[int, int, int]]]]] = queue.Queue()
        self._audio_stop = threading.Event()
        self._current_priority: Optional[int] = None
        self._priority_lock = threading.Lock()
        self._audio_thread: Optional[threading.Thread] = None
        if winsound:
            self._audio_thread = threading.Thread(target=self._audio_worker, daemon=True)
            self._audio_thread.start()

    def _audio_worker(self) -> None:
        """Dedicated audio thread that processes the queue."""
        while not self._audio_stop.is_set():
            try:
                item = self._audio_queue.get(timeout=0.05)
                if item is None:  # Poison pill to stop
                    break
                priority, pattern = item
                with self._priority_lock:
                    self._current_priority = priority
                for frequency, duration_ms, gap_ms in pattern:
                    if self._audio_stop.is_set():
                        break
                    # Check if a higher priority cue is waiting
                    with self._priority_lock:
                        if self._current_priority is not None and priority > self._current_priority:
                            break  # Preempted by higher priority
                    winsound.Beep(frequency, duration_ms)
                    if gap_ms:
                        time.sleep(gap_ms / 1000.0)
                with self._priority_lock:
                    self._current_priority = None
            except queue.Empty:
                continue

    def reset(self) -> None:
        self._stage_state.clear()
        self._last_dist_to_event.clear()
        self._last_lap_pct = None
        self._lap_id = 0
        # Clear audio queue on reset
        self._clear_audio_queue()

    def update(
        self,
        lap_pct: float,
        track_len_m: Optional[float],
        events: List[RefEvent],
        approach_a_s: float,
        approach_b_s: float,
        final_cue_offset_m: float,
        enable_brake: bool,
        enable_lift: bool,
        enable_power: bool,
        quiet_mode: bool,
        speed_mps: Optional[float] = None,
    ) -> None:
        if lap_pct is None:
            return
        if self._last_lap_pct is not None and lap_pct < self._last_lap_pct - 0.5:
            self._lap_id += 1
            self._stage_state.clear()
            self._last_dist_to_event.clear()
        self._last_lap_pct = lap_pct

        if not events:
            return

        if track_len_m and track_len_m > 1.0:
            approach_a_m, approach_b_m = self._approach_distances(speed_mps, approach_a_s, approach_b_s)
            current_dist_m = lap_pct * track_len_m
            for idx, event in enumerate(events):
                if event.dist_m is None:
                    continue
                if event.kind == "brake" and not enable_brake:
                    continue
                if event.kind == "lift" and not enable_lift:
                    continue
                if event.kind == "power" and not enable_power:
                    continue
                dist_to_event = (event.dist_m - current_dist_m) % track_len_m
                last_dist = self._last_dist_to_event.get(idx)

                self._handle_stage(
                    idx,
                    event,
                    dist_to_event,
                    last_dist,
                    approach_a_m,
                    approach_b_m,
                    final_cue_offset_m,
                    quiet_mode,
                )

                if last_dist is not None and last_dist <= approach_b_m and dist_to_event > last_dist:
                    self._handle_stage(
                        idx,
                        event,
                        0.0,
                        last_dist,
                        approach_a_m,
                        approach_b_m,
                        final_cue_offset_m,
                        quiet_mode,
                        force_c=True,
                    )

                self._last_dist_to_event[idx] = dist_to_event
        else:
            approach_a_pct = 0.02
            approach_b_pct = 0.01
            for idx, event in enumerate(events):
                if event.kind == "brake" and not enable_brake:
                    continue
                if event.kind == "lift" and not enable_lift:
                    continue
                if event.kind == "power" and not enable_power:
                    continue
                delta_pct = (event.lap_pct - lap_pct) % 1.0
                last_pct = self._last_dist_to_event.get(idx)
                self._handle_stage(
                    idx,
                    event,
                    delta_pct,
                    last_pct,
                    approach_a_pct,
                    approach_b_pct,
                    final_cue_offset_m,
                    quiet_mode,
                    pct_mode=True,
                )
                if last_pct is not None and last_pct <= approach_b_pct and delta_pct > last_pct:
                    self._handle_stage(
                        idx,
                        event,
                        0.0,
                        last_pct,
                        approach_a_pct,
                        approach_b_pct,
                        final_cue_offset_m,
                        quiet_mode,
                        pct_mode=True,
                        force_c=True,
                    )
                self._last_dist_to_event[idx] = delta_pct

    def _approach_distances(
        self,
        speed_mps: Optional[float],
        approach_a_s: float,
        approach_b_s: float,
    ) -> Tuple[float, float]:
        """Translate approach timing into distance so beeps scale with speed."""
        speed = speed_mps if speed_mps and speed_mps > 0 else DEFAULT_APPROACH_SPEED_MPS
        approach_a_m = max(1.0, speed * approach_a_s)
        approach_b_m = max(0.5, speed * approach_b_s)
        if approach_b_m >= approach_a_m:
            approach_b_m = max(0.5, approach_a_m * 0.5)
        return approach_a_m, approach_b_m

    def _handle_stage(
        self,
        idx: int,
        event: RefEvent,
        distance: float,
        last_distance: Optional[float],
        approach_a: float,
        approach_b: float,
        final_cue_offset_m: float,
        quiet_mode: bool,
        pct_mode: bool = False,
        force_c: bool = False,
    ) -> None:
        state = self._stage_state.setdefault(idx, {"a": False, "b": False, "c": False})

        def trigger(stage: str) -> None:
            if state[stage]:
                return
            state[stage] = True
            self._play_pattern(self._pattern_for(event.kind, stage), quiet_mode, stage)

        def crossed(threshold: float) -> bool:
            if last_distance is None:
                return distance <= threshold
            return last_distance > threshold >= distance

        if force_c:
            trigger("c")
            return

        if crossed(approach_a) and not state["a"]:
            trigger("a")
        if crossed(approach_b) and not state["b"]:
            trigger("b")
        final_threshold = 0.001 if pct_mode else max(0.0, final_cue_offset_m)
        if crossed(final_threshold) and not state["c"]:
            trigger("c")

    def _pattern_for(self, kind: str, stage: str) -> List[Tuple[int, int, int]]:
        if kind == "lift":
            patterns = {
                "a": [(520, 80, 0)],
                "b": [(700, 110, 0)],
                "c": [(980, 190, 0)],
            }
        elif kind == "power":
            patterns = {
                "a": [(640, 80, 0)],
                "b": [(860, 110, 0)],
                "c": [(1200, 190, 0)],
            }
        else:
            patterns = {
                "a": [(560, 90, 0)],
                "b": [(820, 130, 0)],
                "c": [(1300, 700, 0)],
            }
        return patterns[stage]

    def _clear_audio_queue(self) -> None:
        """Clear all pending audio cues from the queue."""
        try:
            while True:
                self._audio_queue.get_nowait()
        except queue.Empty:
            pass

    def _play_pattern(self, pattern: List[Tuple[int, int, int]], quiet_mode: bool, stage: str = "c") -> None:
        if quiet_mode:
            scaled = []
            for freq, duration, gap in pattern:
                scaled.append((freq, max(40, int(duration * 0.6)), int(gap * 0.6)))
            pattern = scaled

        # Map stage to priority (lower = higher priority)
        priority_map = {"c": self._PRIORITY_C, "b": self._PRIORITY_B, "a": self._PRIORITY_A}
        priority = priority_map.get(stage, self._PRIORITY_A)

        if winsound:
            # For high priority cues, clear lower priority pending cues
            if priority <= self._PRIORITY_B:
                self._clear_audio_queue()
                # Signal preemption to current playing cue
                with self._priority_lock:
                    self._current_priority = priority

            # Add to queue for dedicated audio thread to process
            self._audio_queue.put((priority, pattern))
        else:
            self._root.bell()


class OverlayWindow(tk.Toplevel):
    def __init__(self, root: tk.Tk, width: int, height: int) -> None:
        super().__init__(root)
        self.title("Nishizumi IBT")
        self.configure(bg="#0b0b0b")
        self.geometry(f"{width}x{height}+100+100")
        self.attributes("-topmost", True)
        self.attributes("-alpha", DEFAULT_ALPHA)
        self.overrideredirect(True)
        self._drag_start = None
        self._resize_mode = False

        self.canvas = tk.Canvas(self, bg="#0b0b0b", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        
    def toggle_resize_mode(self, enabled: bool) -> None:
        """enabled=True -> native window frame and resize handles; enabled=False -> frameless locked overlay."""
        self._resize_mode = enabled

        # When enabled, let the OS draw a normal resizable window border.
        self.overrideredirect(not enabled)
        self.resizable(enabled, enabled)

        # Re-apply attributes that might get reset by overrideredirect changes.
        self.attributes("-topmost", True)
        self.attributes("-alpha", DEFAULT_ALPHA)

    def set_size(self, width: int, height: int) -> None:
        # Only force size if not in native resize mode
        if self._resize_mode:
            return
            
        size, _, position = self.geometry().partition("+")
        x_str, _, y_str = position.partition("+")
        x = int(x_str) if x_str else 100
        y = int(y_str) if y_str else 100
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _start_drag(self, event: tk.Event) -> None:
        # Disable dragging when in native resize mode (let window manager handle it)
        if self._resize_mode:
            return
        self._drag_start = (event.x_root, event.y_root)

    def _on_drag(self, event: tk.Event) -> None:
        if self._resize_mode or not self._drag_start:
            return
        x_root, y_root = self._drag_start
        dx = event.x_root - x_root
        dy = event.y_root - y_root
        geom = self.geometry()
        size, _, position = geom.partition("+")
        x_str, _, y_str = position.partition("+")
        x = int(x_str) + dx
        y = int(y_str) + dy
        self.geometry(f"{size}+{x}+{y}")
        self._drag_start = (event.x_root, event.y_root)

    def draw(
        self,
        snapshot: TelemetrySnapshot,
        ref: Optional[ReferenceLap],
        samples: Deque[Dict[str, Optional[float]]],
        flow_window_s: float,
        lookahead_window_s: float,
        speed_delta_kph: Optional[float],
        steering_deg: Optional[float],
        gear: Optional[int],
        gear_hint: str,
        lap_pct: Optional[float],
        track_len_m: Optional[float],
        lookahead_m: float,
        ref_lead_s: float = 0.0,
        show_live_throttle: bool = True,
        show_live_brake: bool = True,
        show_ref_throttle: bool = True,
        show_ref_brake: bool = True,
    ) -> None:
        self.canvas.delete("all")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()

        y_cursor = 12
        self._draw_delta_bar(width, y_cursor, speed_delta_kph)
        y_cursor += 44

        flow_bottom = min(height - 110, y_cursor + 240)
        if flow_bottom > y_cursor:
            self._draw_flowing_stream(
                width,
                y_cursor,
                flow_bottom,
                samples,
                flow_window_s,
                lookahead_window_s,
                ref is not None,
                show_live_throttle,
                show_live_brake,
                show_ref_throttle,
                show_ref_brake,
                ref_lead_s=ref_lead_s,
            )
            if ref is not None and lap_pct is not None and track_len_m is not None:
                self._draw_lookahead_preview(
                    width,
                    y_cursor,
                    flow_bottom,
                    ref,
                    lap_pct,
                    track_len_m,
                    flow_window_s,
                    lookahead_window_s,
                    show_ref_throttle,
                    show_ref_brake,
                )

        self._draw_gear_steer(width, height, gear, steering_deg, gear_hint)

    def _draw_delta_bar(self, width: int, y: int, speed_delta_kph: Optional[float]) -> None:
        bar_width = width - 40
        bar_x = 20
        bar_y = y
        bar_height = 18
        self.canvas.create_rectangle(bar_x, bar_y, bar_x + bar_width, bar_y + bar_height, outline="#2a2a2a")
        center = bar_x + bar_width / 2
        self.canvas.create_line(center, bar_y, center, bar_y + bar_height, fill="#444444")

        delta = speed_delta_kph or 0.0
        max_delta = 5.0
        scaled = clamp(delta / max_delta, -1.0, 1.0)
        fill_color = "#ffffff"
        if delta > 1.0:
            fill_color = DEFAULT_LIVE_THROTTLE_COLOR  # Consistent green
        elif delta < -1.0:
            fill_color = DEFAULT_LIVE_BRAKE_COLOR  # Consistent red
        fill_width = scaled * (bar_width / 2)
        if fill_width >= 0:
            self.canvas.create_rectangle(center, bar_y, center + fill_width, bar_y + bar_height, fill=fill_color, width=0)
        else:
            self.canvas.create_rectangle(center + fill_width, bar_y, center, bar_y + bar_height, fill=fill_color, width=0)
        self.canvas.create_text(
            center,
            bar_y + bar_height / 2,
            text=f"Δ {delta:+.1f} kph",
            fill="#ffffff",
            font=("Segoe UI", 12, "bold"),
        )

    def _draw_grid_background(
        self,
        origin_x: int,
        top: int,
        right_x: float,
        bottom: int,
        height: int,
    ) -> None:
        """Draw a subtle professional grid background."""
        # Horizontal grid lines (0%, 25%, 50%, 75%, 100%)
        for i in range(5):
            y = bottom - (i / 4.0) * height
            color = DEFAULT_GRID_ACCENT_COLOR if i == 2 else DEFAULT_GRID_COLOR
            width = 1 if i == 2 else 1
            self.canvas.create_line(
                origin_x, y, right_x, y,
                fill=color,
                width=width,
            )

        # Vertical grid lines (every 2 seconds of the 10-second window)
        for i in range(6):  # 0, 2, 4, 6, 8, 10 seconds
            progress = i / 5.0
            x = origin_x + progress * (right_x - origin_x)
            self.canvas.create_line(
                x, top, x, bottom,
                fill=DEFAULT_GRID_COLOR,
                width=1,
            )

    def _build_line_points(
        self,
        window: List[Dict[str, Optional[float]]],
        key: str,
        origin_x: int,
        bottom: int,
        height: int,
        cutoff: float,
        flow_window_s: float,
        split_x: float,
        series_offset_s: float = 0.0,
    ) -> List[List[float]]:
        """Build point sequences for a data series, splitting on None values.

        series_offset_s lets us visually lead/lag this series in time (seconds).
        """
        segments: List[List[float]] = []
        current_points: List[float] = []

        for sample in window:
            value = sample.get(key)
            if value is None:
                if len(current_points) >= 4:
                    segments.append(current_points)
                current_points = []
                continue
            t = sample["t"]
            if t is None:
                continue
            # Apply per-series horizontal offset: positive = draw earlier (to the left)
            effective_t = t - series_offset_s
            x = origin_x + ((effective_t - cutoff) / flow_window_s) * (split_x - origin_x)
            normalized = clamp(value, 0.0, 1.0)
            y = bottom - normalized * height
            current_points.extend([x, y])

        if len(current_points) >= 4:
            segments.append(current_points)

        return segments

    def _draw_filled_area(
        self,
        points: List[float],
        bottom: int,
        fill_color: str,
    ) -> None:
        """Draw a filled polygon under the line for area effect."""
        if len(points) < 4:
            return

        # Create polygon points: line points + bottom corners
        polygon_points = list(points)
        # Add bottom-right corner
        polygon_points.extend([points[-2], bottom])
        # Add bottom-left corner
        polygon_points.extend([points[0], bottom])

        self.canvas.create_polygon(
            polygon_points,
            fill=fill_color,
            outline="",
            smooth=True,
            splinesteps=12,
        )

    def _draw_glowing_line(
        self,
        points: List[float],
        color: str,
        glow_color: str,
        base_width: float = 2.0,
        glow_layers: int = 3,
        smooth: bool = True,
    ) -> None:
        """Draw a line with professional glow effect."""
        if len(points) < 4:
            return

        # Draw glow layers (outer to inner)
        for i in range(glow_layers, 0, -1):
            glow_width = base_width + (i * 2.5)
            # Calculate glow opacity (simulated with color blending)
            opacity_factor = 0.15 / i
            glow_col = self._blend_color(glow_color, "#0b0b0b", opacity_factor)

            self.canvas.create_line(
                points,
                fill=glow_col,
                width=glow_width,
                capstyle=tk.ROUND,
                joinstyle=tk.ROUND,
                smooth=smooth,
                splinesteps=12,
            )

        # Draw main line
        self.canvas.create_line(
            points,
            fill=color,
            width=base_width,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
            smooth=smooth,
            splinesteps=12,
        )

    def _blend_color(self, color1: str, color2: str, factor: float) -> str:
        """Blend two hex colors together. Factor 1.0 = full color1, 0.0 = full color2."""
        # Parse hex colors
        r1, g1, b1 = int(color1[1:3], 16), int(color1[3:5], 16), int(color1[5:7], 16)
        r2, g2, b2 = int(color2[1:3], 16), int(color2[3:5], 16), int(color2[5:7], 16)

        # Blend
        r = int(r1 * factor + r2 * (1 - factor))
        g = int(g1 * factor + g2 * (1 - factor))
        b = int(b1 * factor + b2 * (1 - factor))

        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw_flowing_stream(
        self,
        width: int,
        top: int,
        bottom: int,
        samples: Deque[Dict[str, Optional[float]]],
        flow_window_s: float,
        lookahead_window_s: float,
        show_reference: bool,
        show_live_throttle: bool = True,
        show_live_brake: bool = True,
        show_ref_throttle: bool = True,
        show_ref_brake: bool = True,
        ref_lead_s: float = 0.0,
    ) -> None:
        if not samples:
            return
        flow_window_s = max(0.5, flow_window_s)
        now = samples[-1]["t"]
        cutoff = now - flow_window_s
        window = [sample for sample in samples if sample["t"] is not None and sample["t"] >= cutoff]
        if len(window) < 2:
            return

        origin_x = 20
        flow_width = max(40, width - 40)
        height = max(40, bottom - top)

        history_ratio = flow_window_s / (flow_window_s + lookahead_window_s)
        split_x = origin_x + (flow_width * history_ratio)

        # Draw professional grid background
        self._draw_grid_background(origin_x, top, split_x, bottom, height)

        # Draw the split line (now/future separator)
        self.canvas.create_line(
            split_x, top, split_x, bottom,
            fill="#404040",
            width=2,
        )

        # Draw reference lines first (behind live lines)
        if show_reference:
            if show_ref_throttle:
                segments = self._build_line_points(
                    window, "ref_throttle", origin_x, bottom, height,
                    cutoff, flow_window_s, split_x,
                    series_offset_s=ref_lead_s,
                )
                for points in segments:
                    self.canvas.create_line(
                        points,
                        fill=DEFAULT_REF_THROTTLE_COLOR,
                        width=2.5,
                        dash=(10, 5),
                        capstyle=tk.ROUND,
                        joinstyle=tk.ROUND,
                        smooth=True,
                        splinesteps=12,
                    )

            if show_ref_brake:
                segments = self._build_line_points(
                    window, "ref_brake", origin_x, bottom, height,
                    cutoff, flow_window_s, split_x,
                    series_offset_s=ref_lead_s,
                )
                for points in segments:
                    self.canvas.create_line(
                        points,
                        fill=DEFAULT_REF_BRAKE_COLOR,
                        width=2.5,
                        dash=(10, 5),
                        capstyle=tk.ROUND,
                        joinstyle=tk.ROUND,
                        smooth=True,
                        splinesteps=12,
                    )

        # Draw live telemetry with glow and fill effects
        if show_live_throttle:
            segments = self._build_line_points(
                window, "throttle", origin_x, bottom, height,
                cutoff, flow_window_s, split_x,
            )
            for points in segments:
                # Draw glowing line (no fill)
                self._draw_glowing_line(
                    points,
                    DEFAULT_LIVE_THROTTLE_COLOR,
                    DEFAULT_LIVE_THROTTLE_GLOW,
                    base_width=2.5,
                    glow_layers=3,
                )

        if show_live_brake:
            segments = self._build_line_points(
                window, "brake", origin_x, bottom, height,
                cutoff, flow_window_s, split_x,
            )
            for points in segments:
                # Draw glowing line (no fill)
                self._draw_glowing_line(
                    points,
                    DEFAULT_LIVE_BRAKE_COLOR,
                    DEFAULT_LIVE_BRAKE_GLOW,
                    base_width=2.5,
                    glow_layers=3,
                )

    def _draw_lookahead_preview(
        self,
        width: int,
        top: int,
        bottom: int,
        ref: ReferenceLap,
        lap_pct: float,
        track_len_m: float,
        flow_window_s: float,
        lookahead_window_s: float,
        show_ref_throttle: bool = True,
        show_ref_brake: bool = True,
    ) -> None:
        """Draw the lookahead preview (next N seconds of IBT reference) with professional styling."""
        origin_x = 20
        flow_width = max(40, width - 40)
        height = max(40, bottom - top)

        history_ratio = flow_window_s / (flow_window_s + lookahead_window_s)
        split_x = origin_x + (flow_width * history_ratio)
        preview_width = flow_width * (1 - history_ratio)
        right_x = origin_x + flow_width

        if preview_width < 10 or track_len_m <= 0:
            return

        # Draw grid for lookahead section
        self._draw_grid_background(int(split_x), top, right_x, bottom, height)

        samples_count = 36  # More samples for smoother curves
        current_speed = ref.ref_at_pct(ref.speed, lap_pct)
        if current_speed < 1:
            current_speed = 50

        lookahead_distance = current_speed * lookahead_window_s

        throttle_points: List[float] = []
        brake_points: List[float] = []

        for i in range(samples_count):
            progress = i / (samples_count - 1)
            distance_ahead = progress * lookahead_distance
            future_pct = (lap_pct + (distance_ahead / track_len_m)) % 1.0

            throttle_val = ref.ref_at_pct(ref.throttle, future_pct)
            brake_val = ref.ref_at_pct(ref.brake, future_pct)

            x = split_x + (progress * preview_width)

            throttle_norm = clamp(throttle_val, 0.0, 1.0)
            brake_norm = clamp(brake_val, 0.0, 1.0)

            throttle_y = bottom - throttle_norm * height
            brake_y = bottom - brake_norm * height

            throttle_points.extend([x, throttle_y])
            brake_points.extend([x, brake_y])

        # Draw lookahead lines with dotted style (matching reference lines)
        if show_ref_throttle and len(throttle_points) >= 4:
            self.canvas.create_line(
                throttle_points,
                fill="#00cc00",  # Slightly dimmer green for preview
                width=2.5,
                dash=(10, 5),
                capstyle=tk.ROUND,
                joinstyle=tk.ROUND,
                smooth=True,
                splinesteps=12,
            )

        if show_ref_brake and len(brake_points) >= 4:
            self.canvas.create_line(
                brake_points,
                fill="#cc2222",  # Slightly dimmer red for preview
                width=2.5,
                dash=(10, 5),
                capstyle=tk.ROUND,
                joinstyle=tk.ROUND,
                smooth=True,
                splinesteps=12,
            )

    def _draw_gear_steer(
        self,
        width: int,
        height: int,
        gear: Optional[int],
        steering_deg: Optional[float],
        gear_hint: str,
    ) -> None:
        if gear is None:
            return
        radius = 26
        cx = 60
        cy = height - 70
        color = "#ffffff"
        if gear_hint == "match":
            color = DEFAULT_LIVE_THROTTLE_COLOR  # Use consistent green
        elif gear_hint == "mismatch":
            color = DEFAULT_LIVE_BRAKE_COLOR  # Use consistent red

        self.canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline=color, width=3)
        self.canvas.create_text(cx, cy, text=str(gear), fill=color, font=("Segoe UI", 18, "bold"))

        steer_text = f"{steering_deg:+.0f}°" if steering_deg is not None else "--"
        self.canvas.create_rectangle(width - 140, height - 90, width - 20, height - 50, outline="#ffffff")
        self.canvas.create_text(
            width - 80,
            height - 70,
            text=steer_text,
            fill="#ffffff",
            font=("Segoe UI", 12, "bold"),
        )

class MemoryLogHandler(logging.Handler):
    def __init__(self, max_entries: int = 200) -> None:
        super().__init__()
        self.entries: Deque[str] = deque(maxlen=max_entries)

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self.entries.append(msg)


class NishizumiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.logger = logging.getLogger("nishizumi")
        self.logger.setLevel(logging.INFO)
        self.log_handler = MemoryLogHandler()
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        self.logger.addHandler(self.log_handler)

        self.worker = TelemetryWorker(self.logger)
        self.worker.start()

        self.reference: Optional[ReferenceLap] = None
        self.samples: Deque[Dict[str, Optional[float]]] = deque(maxlen=1000)
        self.overlay: Optional[OverlayWindow] = None
        self.audio = AudioCues(root, self.logger)

        self.last_live_lap_pct: Optional[float] = None
        self.live_unwrapped_m: Optional[float] = None
        self.last_track_len_m: Optional[float] = None
        self.last_gear: Optional[int] = None

        self._build_ui()
        self.root.bind_all("<Control-Shift-O>", lambda _evt: self._toggle_overlay())
        self.root.bind_all("<Escape>", lambda _evt: self._toggle_overlay(force_hide=True))
        self.root.after(DEFAULT_UPDATE_MS, self._update)

    def _build_ui(self) -> None:
        self.ibt_path_var = tk.StringVar()
        self.brake_threshold_var = tk.DoubleVar(value=DEFAULT_BRAKE_THRESHOLD)
        self.lift_threshold_var = tk.DoubleVar(value=DEFAULT_LIFT_THRESHOLD)
        self.power_threshold_var = tk.DoubleVar(value=DEFAULT_POWER_THRESHOLD)
        self.approach_a_var = tk.DoubleVar(value=DEFAULT_APPROACH_A_S)
        self.approach_b_var = tk.DoubleVar(value=DEFAULT_APPROACH_B_S)
        self.final_cue_offset_var = tk.DoubleVar(value=DEFAULT_FINAL_CUE_OFFSET_M)
        self.update_ms_var = tk.IntVar(value=DEFAULT_UPDATE_MS)
        self.quiet_mode_var = tk.BooleanVar(value=False)
        self.overlay_width_var = tk.IntVar(value=DEFAULT_OVERLAY_WIDTH)
        self.overlay_height_var = tk.IntVar(value=DEFAULT_OVERLAY_HEIGHT)
        self.lookahead_window_var = tk.DoubleVar(value=DEFAULT_LOOKAHEAD_WINDOW_S)
        self.overlay_locked_var = tk.BooleanVar(value=True)

        # New: reference lead time (seconds) to show reference traces earlier
        self.ref_lead_s_var = tk.DoubleVar(value=0.0)

        self.overlay_enabled_var = tk.BooleanVar(value=True)

        self.audio_brake_var = tk.BooleanVar(value=True)
        self.audio_lift_var = tk.BooleanVar(value=False)
        self.audio_power_var = tk.BooleanVar(value=False)
        self.audio_gear_beep_var = tk.BooleanVar(value=True)
        self.show_live_throttle_var = tk.BooleanVar(value=True)
        self.show_live_brake_var = tk.BooleanVar(value=True)
        self.show_ref_throttle_var = tk.BooleanVar(value=True)
        self.show_ref_brake_var = tk.BooleanVar(value=True)

        self.status_var = tk.StringVar(value="Load a reference IBT file to begin.")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True)

        settings = ttk.Frame(notebook, padding=12)
        debug = ttk.Frame(notebook, padding=12)
        notebook.add(settings, text="Settings")
        notebook.add(debug, text="Debug")

        row = 0
        ttk.Label(settings, text="Reference IBT:").grid(row=row, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.ibt_path_var, width=46).grid(row=row, column=1, sticky="ew")
        ttk.Button(settings, text="Browse", command=self._browse_ibt).grid(row=row, column=2, padx=6)
        row += 1

        ttk.Label(settings, text="Brake threshold:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(settings, textvariable=self.brake_threshold_var, width=8).grid(row=row, column=1, sticky="w", pady=(6, 0))
        row += 1

        ttk.Label(settings, text="Lift threshold:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(settings, textvariable=self.lift_threshold_var, width=8).grid(row=row, column=1, sticky="w", pady=(6, 0))
        row += 1

        ttk.Label(settings, text="Power threshold:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(settings, textvariable=self.power_threshold_var, width=8).grid(row=row, column=1, sticky="w", pady=(6, 0))
        row += 1

        ttk.Label(settings, text="Approach times (s):").grid(row=row, column=0, sticky="w", pady=(6, 0))
        approach_frame = ttk.Frame(settings)
        approach_frame.grid(row=row, column=1, sticky="w", pady=(6, 0))
        ttk.Entry(approach_frame, textvariable=self.approach_a_var, width=6).grid(row=0, column=0)
        ttk.Entry(approach_frame, textvariable=self.approach_b_var, width=6).grid(row=0, column=1, padx=6)
        row += 1

        ttk.Label(settings, text="Update rate (ms):").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(settings, textvariable=self.update_ms_var, width=8).grid(row=row, column=1, sticky="w", pady=(6, 0))
        row += 1

        ttk.Label(settings, text="Overlay size (W x H):").grid(row=row, column=0, sticky="w", pady=(6, 0))
        size_frame = ttk.Frame(settings)
        size_frame.grid(row=row, column=1, sticky="w", pady=(6, 0))
        ttk.Entry(size_frame, textvariable=self.overlay_width_var, width=6).grid(row=0, column=0)
        ttk.Entry(size_frame, textvariable=self.overlay_height_var, width=6).grid(row=0, column=1, padx=6)
        row += 1
        
        ttk.Label(settings, text="Lookahead (s):").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(settings, textvariable=self.lookahead_window_var, width=8).grid(row=row, column=1, sticky="w", pady=(6, 0))
        row += 1

        # New: reference lead control
        ttk.Label(settings, text="Reference lead (s):").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(settings, textvariable=self.ref_lead_s_var, width=8).grid(
            row=row, column=1, sticky="w", pady=(6, 0)
        )
        row += 1

        ttk.Checkbutton(settings, text="Enable overlay", variable=self.overlay_enabled_var, command=self._toggle_overlay).grid(
            row=row, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(settings, text="Lock Overlay (No Resize)", variable=self.overlay_locked_var, command=self._toggle_overlay_lock).grid(
            row=row, column=1, sticky="w", pady=(8, 0)
        )
        row += 1

        audio_frame = ttk.LabelFrame(settings, text="Audio", padding=8)
        audio_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(audio_frame, text="Brake cues", variable=self.audio_brake_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(audio_frame, text="Lift cues", variable=self.audio_lift_var).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(audio_frame, text="Power cues", variable=self.audio_power_var).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(audio_frame, text="Quiet mode", variable=self.quiet_mode_var).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(audio_frame, text="Gear change beep", variable=self.audio_gear_beep_var).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(audio_frame, text="Final cue offset (m):").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Entry(audio_frame, textvariable=self.final_cue_offset_var, width=6).grid(
            row=2, column=1, sticky="w", pady=(6, 0)
        )
        row += 1

        lines_frame = ttk.LabelFrame(settings, text="Visible Lines", padding=8)
        lines_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        ttk.Label(lines_frame, text="Live:").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Checkbutton(lines_frame, text="Throttle", variable=self.show_live_throttle_var).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Checkbutton(lines_frame, text="Brake", variable=self.show_live_brake_var).grid(
            row=0, column=2, sticky="w"
        )

        ttk.Label(lines_frame, text="Reference:").grid(row=1, column=0, sticky="w", pady=(6, 0), padx=(0, 10))
        ttk.Checkbutton(lines_frame, text="Throttle", variable=self.show_ref_throttle_var).grid(
            row=1, column=1, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(lines_frame, text="Brake", variable=self.show_ref_brake_var).grid(
            row=1, column=2, sticky="w", pady=(6, 0)
        )

        row += 1

        ttk.Label(settings, textvariable=self.status_var, foreground="#6b6b6b").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(12, 0)
        )

        settings.columnconfigure(1, weight=1)

        self.debug_text = scrolledtext.ScrolledText(debug, height=16, width=80, state="disabled")
        self.debug_text.pack(fill=tk.BOTH, expand=True)

    def _browse_ibt(self) -> None:
        path = filedialog.askopenfilename(
            title="Select IBT file", filetypes=[("IBT Files", "*.ibt"), ("All Files", "*")]
        )
        if not path:
            return
        self.ibt_path_var.set(path)
        self._load_reference(path)

    def _load_reference(self, path: str) -> None:
        try:
            self.reference = ReferenceLap(
                path,
                brake_threshold=self.brake_threshold_var.get(),
                lift_threshold=self.lift_threshold_var.get(),
                power_threshold=self.power_threshold_var.get(),
            )
            self.logger.info("Loaded reference IBT: %s", path)
        except Exception as exc:
            messagebox.showerror("Failed to load IBT", str(exc))
            self.reference = None
            return
        self.status_var.set("Reference loaded. Connect to iRacing for live sync.")

    def _toggle_overlay(self, force_hide: bool = False) -> None:
        if force_hide or not self.overlay_enabled_var.get():
            if self.overlay:
                self.overlay.withdraw()
        else:
            self._ensure_overlay()
            if self.overlay:
                self._apply_overlay_size()
                self._toggle_overlay_lock() # Apply current lock state
                self.overlay.deiconify()
                
    def _toggle_overlay_lock(self) -> None:
        if self.overlay:
            is_locked = self.overlay_locked_var.get()
            self.overlay.toggle_resize_mode(not is_locked)

    def _ensure_overlay(self) -> None:
        if not self.overlay:
            self.overlay = OverlayWindow(self.root, *DEFAULT_OVERLAY_SIZE)
            self.overlay.withdraw()

    def _update(self) -> None:
        update_ms = max(5, int(self.update_ms_var.get()))
        snapshot = self.worker.get_snapshot()
        now = time.time()

        if self.overlay_enabled_var.get():
            self._ensure_overlay()
            self._apply_overlay_size()

        if self.reference:
            self.reference.refresh_thresholds(
                self.brake_threshold_var.get(),
                self.lift_threshold_var.get(),
                self.power_threshold_var.get(),
            )

        if not snapshot.connected:
            self.status_var.set("Waiting for iRacing telemetry...")
            self.last_live_lap_pct = None
            self.live_unwrapped_m = None
            self.last_gear = None
            self._update_debug()
            self._schedule_next(update_ms)
            return

        if snapshot.lap_pct is None:
            self.status_var.set("Telemetry connected, waiting for data...")
            self.last_live_lap_pct = None
            self.live_unwrapped_m = None
            self.last_gear = None
            self._update_debug()
            self._schedule_next(update_ms)
            return

        lap_pct = snapshot.lap_pct
        if lap_pct > 1.5:
            lap_pct = lap_pct / 100.0
        throttle = snapshot.throttle or 0.0
        brake = snapshot.brake or 0.0

        speed_kph = (snapshot.speed_mps or 0.0) * 3.6
        steering_deg = snapshot.steering * 57.2958 if snapshot.steering is not None else None
        lookahead_m = DEFAULT_LOOKAHEAD_DISTANCE_M
        if snapshot.speed_mps is not None:
            lookahead_m = clamp(
                snapshot.speed_mps * DEFAULT_FLOW_WINDOW_S,
                DEFAULT_LOOKAHEAD_MIN_M,
                DEFAULT_LOOKAHEAD_MAX_M,
            )

        speed_delta_kph = None
        gear_hint = ""
        ref_speed_mps = None
        ref_throttle = None
        ref_brake = None
        if self.reference:
            ref_speed_mps = self.reference.ref_at_pct(self.reference.speed, lap_pct)
            ref_speed_kph = (ref_speed_mps or 0.0) * 3.6
            speed_delta_kph = speed_kph - ref_speed_kph
            ref_throttle = self.reference.ref_at_pct(self.reference.throttle, lap_pct)
            ref_brake = self.reference.ref_at_pct(self.reference.brake, lap_pct)
            ref_gear = self.reference.ref_gear_at_pct(lap_pct)
            if snapshot.gear is not None:
                gear_hint = "match" if snapshot.gear == ref_gear else "mismatch"

        track_len_m = snapshot.track_length_km * 1000.0 if snapshot.track_length_km else None
        if track_len_m:
            self.last_track_len_m = track_len_m
        track_len_display_m = track_len_m or self.last_track_len_m
        if not track_len_display_m and self.reference and self.reference.track_length_m:
            track_len_display_m = self.reference.track_length_m
            self.last_track_len_m = track_len_display_m
        trace_track_len_m = track_len_display_m or ASSUMED_TRACK_LEN_M
        live_unwrapped_m = self._update_live_unwrapped(lap_pct, trace_track_len_m)
        if live_unwrapped_m is not None:
            self.samples.append(
                {
                    "t": now,
                    "throttle": throttle,
                    "brake": brake,
                    "speed": speed_kph,
                    "ref_throttle": ref_throttle,
                    "ref_brake": ref_brake,
                    "ref_speed": (ref_speed_mps or 0.0) * 3.6 if ref_speed_mps is not None else None,
                }
            )

        if self.overlay_enabled_var.get() and self.overlay:
            self.overlay.draw(
                snapshot=snapshot,
                ref=self.reference,
                samples=self.samples,
                flow_window_s=DEFAULT_FLOW_WINDOW_S,
                lookahead_window_s=self.lookahead_window_var.get(),
                speed_delta_kph=speed_delta_kph,
                steering_deg=steering_deg,
                gear=snapshot.gear,
                gear_hint=gear_hint,
                lap_pct=lap_pct,
                track_len_m=track_len_display_m,
                lookahead_m=lookahead_m,
                ref_lead_s=float(self.ref_lead_s_var.get()),
                show_live_throttle=self.show_live_throttle_var.get(),
                show_live_brake=self.show_live_brake_var.get(),
                show_ref_throttle=self.show_ref_throttle_var.get(),
                show_ref_brake=self.show_ref_brake_var.get(),
            )
            self.overlay.deiconify()
        elif self.overlay:
            self.overlay.withdraw()

        self._maybe_play_gear_beep(snapshot.gear)

        if self.reference:
            if track_len_display_m:
                self.reference.set_event_distances(track_len_display_m)
            self.audio.update(
                lap_pct=lap_pct,
                track_len_m=track_len_display_m,
                events=self.reference.events,
                approach_a_s=max(0.5, float(self.approach_a_var.get())),
                approach_b_s=max(0.25, float(self.approach_b_var.get())),
                final_cue_offset_m=max(0.0, float(self.final_cue_offset_var.get())),
                enable_brake=self.audio_brake_var.get(),
                enable_lift=self.audio_lift_var.get(),
                enable_power=self.audio_power_var.get(),
                quiet_mode=self.quiet_mode_var.get(),
                speed_mps=snapshot.speed_mps,
            )

        next_brake = self._next_brake_distance(lap_pct, track_len_display_m)
        next_brake_text = f"Next brake {next_brake:.0f}m" if next_brake is not None else "Next brake --"
        track_label = snapshot.track_name or "Unknown track"
        track_len_label = f"{track_len_display_m:.0f}m" if track_len_display_m else "--"
        self.status_var.set(
            f"Telemetry connected | {track_label} ({track_len_label}) | {next_brake_text}"
        )

        self._update_debug()
        self._schedule_next(update_ms)

    def _maybe_play_gear_beep(self, gear: Optional[int]) -> None:
        if not self.audio_gear_beep_var.get():
            self.last_gear = gear
            return
        if gear is None:
            self.last_gear = None
            return
        if self.last_gear is not None and gear != self.last_gear:
            if winsound:
                winsound.Beep(1100, 80)
            else:
                self.root.bell()
        self.last_gear = gear

    def _next_brake_distance(self, lap_pct: float, track_len_m: Optional[float]) -> Optional[float]:
        if not self.reference or not self.reference.brake_points or not track_len_m:
            return None
        current_dist = lap_pct * track_len_m
        dist_list = [p * track_len_m for p in self.reference.brake_points]
        idx = bisect.bisect_left(dist_list, current_dist)
        next_dist = dist_list[idx] if idx < len(dist_list) else dist_list[0]
        distance_to = (next_dist - current_dist) % track_len_m
        return distance_to

    def _update_debug(self) -> None:
        self.debug_text.configure(state="normal")
        self.debug_text.delete("1.0", tk.END)
        for entry in self.log_handler.entries:
            self.debug_text.insert(tk.END, entry + "\\n")
        self.debug_text.configure(state="disabled")

    def _update_live_unwrapped(
        self,
        lap_pct: float,
        track_len_m: float,
    ) -> Optional[float]:
        """FIXED: Simplified position tracking - trust iRacing's lap_pct"""
        if track_len_m <= 0:
            return None

        # Initialize on first call
        if self.last_live_lap_pct is None or self.live_unwrapped_m is None:
            self.last_live_lap_pct = lap_pct
            self.live_unwrapped_m = lap_pct * track_len_m
            return self.live_unwrapped_m

        # Calculate delta, handling lap completion
        delta_pct = lap_pct - self.last_live_lap_pct
        if delta_pct < -0.5:  # Lap completed
            delta_pct += 1.0

        # Update unwrapped distance
        self.live_unwrapped_m += delta_pct * track_len_m
        self.last_live_lap_pct = lap_pct
        return self.live_unwrapped_m

    def _schedule_next(self, update_ms: int) -> None:
        self.root.after(update_ms, self._update)

    def _apply_overlay_size(self) -> None:
        if self.overlay and self.overlay._resize_mode:
            # User is resizing with OS handles; mirror that into the entry fields.
            width = max(1, self.overlay.winfo_width())
            height = max(1, self.overlay.winfo_height())
            self.overlay_width_var.set(width)
            self.overlay_height_var.set(height)
            return
            
        width = max(200, int(self.overlay_width_var.get()))
        height = max(160, int(self.overlay_height_var.get()))
        if self.overlay:
            self.overlay.set_size(width, height)

    def _on_close(self) -> None:
        self.worker.stop()
        if self.overlay:
            self.overlay.destroy()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = NishizumiApp(root)
    app._toggle_overlay()
    root.mainloop()


if __name__ == "__main__":
    main()
