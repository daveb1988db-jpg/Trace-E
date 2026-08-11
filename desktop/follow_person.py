#!/usr/bin/env python3
"""
Trace-E nav follower — motion = human (floor cam ~13–25 cm).

- Primary acquire: frame-diff / absdiff -> blobs -> H1, H2 (left->right)
- Sticky lock: IoU / centroid — stay on chosen Hn when others cross
- YOLO person: optional bonus confirm only (never required)
- No lock -> active SEEK (slow pivot/scan); US close -> back/turn away
- Lock -> follow closely (US-first range + differential drive)
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from cam_hub import CAM_HUB, esp_host

try:
    from yolo_tracker import YOLO_TRACKER
except Exception:  # pragma: no cover
    YOLO_TRACKER = None  # type: ignore

# Drive: stay behind — never charge through legs
FORWARD_FAST = 70
FORWARD_MED = 48
FORWARD_SLOW = 22
TURN_MAX = 32
TURN_GAIN = 85.0
CENTER_OK = 0.12
CENTER_SOFT = 0.28
# Floor-cam: legs fill most of the frame even at 50–80cm — area alone must NOT freeze nav.
AREA_TOO_CLOSE = 0.92
AREA_FOLLOW_OK = 0.78
AREA_FAR = 0.20
# Grace while locked but briefly dark — then SEEK, never freeze forever
LOST_STOP_S = 2.2
LOCK_CLEAR_S = 3.5
DETECT_EVERY = 1
DRIVE_HZ = 10.0
# SR-04 hard bumper (cm) — stop BEFORE furniture/walls (was 28 -> headbutts)
US_STOP_CM = 40.0
US_HOLD_CM = 55.0
US_SEEK_AVOID_CM = 50.0
US_SLOW_CM = 110.0
US_CATCH_CM = 170.0
# HC-SR04 glitch floor only — NEVER wipe real close hits (<8 used to null US)
US_NOISE_CM = 1.5
US_MAX_CM = 400.0
MIC_STOP_LEVEL = 0.85
STUCK_S = 3.5
REVERSE_S = 0.45
REVERSE_PWM = 42
US_REVERSE_S = 0.55
US_REVERSE_PWM = 48
BOX_SMOOTH = 0.55
IOU_REACQUIRE = 0.08
IOU_CROSS_IGNORE = 0.04
# Floor cam: need a real moving leg/body — not carpet flicker / Trace herself.
MOTION_ACQUIRE_MIN = 7.5
MOTION_MIN_AREA = 0.018          # real limb / torso patch
MOTION_MAX_AREA = 0.32           # reject room-wide ego flood
MOTION_DIFF_THRESH = 18
MOTION_MAX_BLOBS = 2             # hard cap: H1 + optional H2 only
MOTION_MIN_SIDE = 28
MOTION_MAX_ASPECT = 8.0          # thin legs ok; reject hairline noise
MOTION_MIN_FILL = 0.14
MOTION_MERGE_IOU = 0.12
MOTION_MERGE_DIST_FRAC = 0.14
MOTION_H2_MIN_SEP_FRAC = 0.24
MOTION_H2_MIN_AREA_RATIO = 0.32
MOTION_EGO_GLOBAL = 6.5          # mean absdiff: above -> seek pivot flood
MOTION_EGO_DIFF_BOOST = 16
MOTION_HIT_FRAMES = 3            # need N detect hits before lock (anti-fake H1)
MOTION_MISS_DROP = 4             # drop lock after N low-motion miss frames
MOTION_LOCK_MIN = 5.0            # locked ROI must still move this much
MOTION_CY_MIN = 0.28             # floor cam: prefer lower frame (legs)
# Active seek — in-place pivot only; US close -> reverse + turn (never creep into wall)
SEEK_TURN = 26
SEEK_FLIP_S = 3.8
SEEK_BACK_PWM = 44
SEEK_BACK_S = 0.70
SEEK_AVOID_TURN = 40
SEEK_LOOK_S = 1.4                # stop long enough to see a kicking leg
SEEK_LOOK_GAP_S = 1.6            # then pivot again


@dataclass
class FollowConfig:
    esp_base: str = "http://192.168.1.104"
    forward_fast: int = FORWARD_FAST
    forward_med: int = FORWARD_MED
    forward_slow: int = FORWARD_SLOW
    turn_max: int = TURN_MAX
    turn_gain: float = TURN_GAIN
    center_ok: float = CENTER_OK
    center_soft: float = CENTER_SOFT
    area_too_close: float = AREA_TOO_CLOSE
    area_follow_ok: float = AREA_FOLLOW_OK
    area_far: float = AREA_FAR
    lost_stop_s: float = LOST_STOP_S
    lock_clear_s: float = LOCK_CLEAR_S
    detect_every: int = DETECT_EVERY
    drive_hz: float = DRIVE_HZ
    mirror_x: bool = False
    us_stop_cm: float = US_STOP_CM
    us_hold_cm: float = US_HOLD_CM
    us_seek_avoid_cm: float = US_SEEK_AVOID_CM
    us_slow_cm: float = US_SLOW_CM
    us_catch_cm: float = US_CATCH_CM
    us_noise_cm: float = US_NOISE_CM
    us_max_cm: float = US_MAX_CM
    use_pc_mic: bool = False  # unused — Trace has onboard MAX4466
    use_esp_mic: bool = True  # fuse ESP mic_level into nav
    mic_stop_level: float = MIC_STOP_LEVEL
    mic_presence_level: float = 0.42  # nearby voice/noise while seeking
    target_human: int = 1
    box_smooth: float = BOX_SMOOTH
    iou_reacquire: float = IOU_REACQUIRE
    iou_cross_ignore: float = IOU_CROSS_IGNORE
    motion_acquire_min: float = MOTION_ACQUIRE_MIN
    seek_turn: int = SEEK_TURN
    seek_flip_s: float = SEEK_FLIP_S


@dataclass
class FollowStatus:
    running: bool = False
    mode: str = "idle"
    esp: str = ""
    left: int = 0
    right: int = 0
    person: bool = False
    cx: float = 0.5
    cy: float = 0.5
    area: float = 0.0
    fps: float = 0.0
    detect_ms: float = 0.0
    drive_ms: float = 0.0
    frames: int = 0
    last_error: str = ""
    message: str = "Nav idle"
    source: str = ""
    boxes: int = 0
    humans: List[Dict[str, Any]] = field(default_factory=list)
    target_human: int = 1
    ultrasonic_cm: Optional[float] = None
    mic_level: float = 0.0
    mic_src: str = ""
    obstacle: str = ""
    sensors: Dict[str, Any] = field(default_factory=dict)
    updated: float = 0.0
    annot_seq: int = 0


def _drive_url(esp_base: str, left: int, right: int) -> str:
    q = urllib.parse.urlencode({"left": int(left), "right": int(right)})
    return f"http://{esp_host(esp_base)}:8765/api/drive?{q}"


def clamp_motor(v: float) -> int:
    return int(max(-100, min(100, round(v))))


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / float(max(1, union))


def _centroid(box: Tuple[int, int, int, int]) -> Tuple[float, float]:
    x, y, bw, bh = box
    return x + bw * 0.5, y + bh * 0.5


def _centroid_dist(
    a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]
) -> float:
    ax, ay = _centroid(a)
    bx, by = _centroid(b)
    return float(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)


def _smooth_box(
    prev: Optional[Tuple[int, int, int, int]],
    new: Tuple[int, int, int, int],
    alpha: float,
) -> Tuple[int, int, int, int]:
    if prev is None or alpha <= 0:
        return new
    a = max(0.0, min(1.0, alpha))
    return (
        int(prev[0] * a + new[0] * (1 - a)),
        int(prev[1] * a + new[1] * (1 - a)),
        int(prev[2] * a + new[2] * (1 - a)),
        int(prev[3] * a + new[3] * (1 - a)),
    )


def _us_valid(us_cm: Optional[float], cfg: FollowConfig) -> bool:
    """True when HC-SR04 reading is usable for obstacle avoidance."""
    return (
        us_cm is not None
        and cfg.us_noise_cm <= float(us_cm) <= cfg.us_max_cm
    )


def follow_behind_drive(
    cx: float, area: float, cfg: FollowConfig, us_cm: Optional[float]
) -> Tuple[int, int, str]:
    """
    Stay JUST BEHIND the human.

    Short / floor cam: bbox area is often huge (legs), so range comes from
    ultrasonic when present. Bbox cx steers.

    HC-SR04 is a HARD fence: ≤stop -> reverse cue, ≤hold -> zero forward.
    Differential sign: person on RIGHT (cx>0.5) -> left wheel faster -> turn right.
    """
    err = cx - 0.5
    if cfg.mirror_x:
        err = -err
    turn = max(-cfg.turn_max, min(cfg.turn_max, err * cfg.turn_gain))
    abs_e = abs(err)
    have_us = _us_valid(us_cm, cfg)

    def motors(fwd: float) -> Tuple[int, int]:
        return clamp_motor(fwd + turn), clamp_motor(fwd - turn)

    def face_or_hold(tag: str) -> Tuple[int, int, str]:
        # At hard fence: never forward. In-place face only if clear of stop band.
        if abs_e > cfg.center_ok and tag.startswith("us-hold"):
            l, r = motors(0)
            return l, r, f"{tag}-face"
        return 0, 0, tag

    if have_us:
        # Hard bumper: signal us-stop so caller applies reverse burst
        if us_cm <= cfg.us_stop_cm:
            return -US_REVERSE_PWM, -US_REVERSE_PWM, "us-stop"
        if us_cm <= cfg.us_hold_cm:
            return face_or_hold("us-hold")
        if us_cm <= cfg.us_slow_cm:
            fwd = cfg.forward_slow if abs_e > cfg.center_soft else cfg.forward_med
            mode = "creep"
        elif us_cm <= cfg.us_catch_cm:
            fwd = cfg.forward_med if abs_e > cfg.center_ok else cfg.forward_fast
            mode = "catch-up"
        else:
            fwd = cfg.forward_fast
            mode = "seek-far"
        if abs_e > cfg.center_soft:
            fwd = max(cfg.forward_slow, int(fwd * 0.55))
            mode = "pivot"
        l, r = motors(fwd)
        return l, r, mode

    if area >= cfg.area_too_close:
        return face_or_hold("heel-hold")
    if area >= cfg.area_follow_ok:
        return face_or_hold("behind-hold")
    if abs_e <= cfg.center_ok:
        fwd = cfg.forward_fast if area < cfg.area_far else cfg.forward_med
        mode = "catch-up"
    elif abs_e <= cfg.center_soft:
        fwd = cfg.forward_med
        mode = "track"
    else:
        fwd = max(cfg.forward_slow, 16)
        mode = "pivot"
    l, r = motors(fwd)
    return l, r, mode


def seek_scan_drive(
    cfg: FollowConfig,
    us_cm: Optional[float],
    seek_dir: int,
    backoff_until: float,
    now: float,
) -> Tuple[int, int, str, bool]:
    """
    Active search when unlocked. Returns (L, R, mode, flip_dir).
    Never drives forward into obstacles — pivot / reverse / turn only.
    flip_dir True -> caller should reverse scan direction after obstacle.
    """
    d = 1 if seek_dir >= 0 else -1
    have_us = _us_valid(us_cm, cfg)
    avoid_cm = max(cfg.us_seek_avoid_cm, cfg.us_hold_cm)

    if now < backoff_until:
        # Reverse while backing off a wall/obstacle
        return -SEEK_BACK_PWM, -SEEK_BACK_PWM, "seek-back", False

    if have_us and us_cm <= cfg.us_stop_cm:
        # Hard bumper: reverse hard + flip scan the other way
        return -SEEK_BACK_PWM, -SEEK_BACK_PWM, "seek-avoid", True

    if have_us and us_cm <= avoid_cm:
        # Too close to roam forward — reverse slightly then in-place turn
        # Prefer reverse first when under hold, else hard pivot away
        if us_cm <= cfg.us_hold_cm:
            turn = SEEK_AVOID_TURN * d
            # reverse + yaw so we don't sit grinding a unit
            back = int(SEEK_BACK_PWM * 0.65)
            return (
                clamp_motor(-back + turn),
                clamp_motor(-back - turn),
                "seek-avoid",
                True,
            )
        turn = SEEK_AVOID_TURN * d
        return clamp_motor(turn), clamp_motor(-turn), "seek-avoid-turn", False

    # Gentle in-place pivot / scan — NO forward component
    turn = int(cfg.seek_turn) * d
    return clamp_motor(turn), clamp_motor(-turn), "seek-scan", False


def parse_human_command(text: str) -> Optional[int]:
    """Return 1..9 if text selects a human, else None."""
    t = (text or "").strip().lower()
    if not t:
        return None
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    m = re.search(
        r"\b(?:human|person|track|follow|target|h)\s*(?:number\s*)?(one|two|three|four|five|[1-9])\b",
        t,
    )
    if m:
        tok = m.group(1)
        if tok in words:
            return words[tok]
        return int(tok)
    m = re.search(r"\bh([1-9])\b", t)
    if m:
        return int(m.group(1))
    return None


parse_human_target = parse_human_command


class SensorProbe:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: Dict[str, Any] = {
            "ultrasonic_cm": None,
            "mic_level": 0.0,
            "mic_src": "",
            "esp_ok": False,
            "available": [],
            "missing": ["ultrasonic"],
        }
        self._pc_mic_ok: Optional[bool] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._esp = "http://192.168.1.104"
        self._us_hold: Optional[float] = None
        self._us_hold_t = 0.0

    def start(self, esp_base: str) -> None:
        self._esp = esp_base.rstrip("/")
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="trace-nav-sensors", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._last)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(0.12)

    def _tick(self) -> None:
        h = esp_host(self._esp)
        us_cm: Optional[float] = None
        mic_level = 0.0
        mic_src = ""
        available: List[str] = ["camera"]
        missing: List[str] = []
        esp_ok = False
        body: Dict[str, Any] = {}

        for url in (f"http://{h}:8765/api/status", f"http://{h}/api/status"):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=0.35) as resp:
                    body = json.loads(resp.read().decode("utf-8", errors="replace"))
                esp_ok = True
                break
            except Exception:
                continue

        if esp_ok:
            for key in ("ultrasonic_cm", "us_cm", "distance_cm", "sonar_cm"):
                if key in body and body[key] is not None:
                    try:
                        v = float(body[key])
                        # Accept any positive HC-SR04 reading in range; do NOT drop close hits
                        if 0.5 <= v <= US_MAX_CM:
                            us_cm = v
                            available.append("ultrasonic")
                            self._us_hold = v
                            self._us_hold_t = time.time()
                            break
                    except (TypeError, ValueError):
                        pass
            if "mic_level" in body and body["mic_level"] is not None:
                try:
                    mic_level = float(body["mic_level"])
                    mic_src = "esp"
                    available.append("mic")
                except (TypeError, ValueError):
                    pass
        # Brief hold of last good US so one failed poll never opens the bumper
        if us_cm is None and self._us_hold is not None and (time.time() - self._us_hold_t) < 0.6:
            us_cm = self._us_hold
            if "ultrasonic" not in available:
                available.append("ultrasonic")
        if us_cm is None:
            missing.append("ultrasonic")

        if not mic_src:
            pc = self._pc_mic_sample()
            if pc is not None:
                mic_level = pc
                mic_src = "pc"
                available.append("mic_pc")

        with self._lock:
            self._last = {
                "ultrasonic_cm": us_cm,
                "mic_level": round(mic_level, 3),
                "mic_src": mic_src,
                "esp_ok": esp_ok,
                "available": available,
                "missing": missing,
            }

    def _pc_mic_sample(self) -> Optional[float]:
        if self._pc_mic_ok is False:
            return None
        try:
            import sounddevice as sd  # type: ignore
        except Exception:
            self._pc_mic_ok = False
            return None
        try:
            self._pc_mic_ok = True
            rec = sd.rec(800, samplerate=16000, channels=1, dtype="float32", blocking=True)
            peak = float(np.max(np.abs(rec))) if rec is not None else 0.0
            return max(0.0, min(1.0, peak * 2.2))
        except Exception:
            self._pc_mic_ok = False
            return None


class PersonFollower:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cfg = FollowConfig()
        self._status = FollowStatus()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._annot_jpeg: Optional[bytes] = None
        self._annot_seq = 0
        self._annot_cond = threading.Condition(self._lock)
        self._prev_gray: Optional[np.ndarray] = None
        self._last_drive = (0, 0)
        self._drive_cmd = (0, 0)
        self._drive_esp = "http://192.168.1.104"
        self._drive_force = threading.Event()
        self._drive_wake = threading.Event()
        self._drive_stop = threading.Event()
        self._drive_thread: Optional[threading.Thread] = None
        self._sensors = SensorProbe()
        self._tracks: Dict[int, Tuple[int, int, int, int]] = {}
        self._target_human = 1
        self._locked_id: Optional[int] = None  # sticky H slot
        self._locked_box: Optional[Tuple[int, int, int, int]] = None
        self._smooth_box: Optional[Tuple[int, int, int, int]] = None
        self._stuck_since: Optional[float] = None
        self._reverse_until = 0.0
        self._us_reverse_until = 0.0
        self._us_prev: Optional[float] = None
        self._seek_dir = 1
        self._seek_flip_at = 0.0
        self._seek_backoff_until = 0.0
        self._detector = "motion"
        self._mog2 = None
        self._last_humans: List[Dict[str, Any]] = []
        self._motion_energy = 0.0
        self._seek_look_until = 0.0
        self._seek_look_next = 0.0
        self._ego_until = 0.0  # brief ego suppress after pivot; off while stopped
        self._listen_hold = False  # cover-sensor talk — freeze motors
        self._acquire_hits = 0
        self._acquire_box: Optional[Tuple[int, int, int, int]] = None
        self._lock_miss = 0
        self._ensure_drive_worker()

    def set_listen_hold(self, on: bool) -> None:
        """Freeze drive while Trace is listening / speaking (cover-US talk)."""
        with self._lock:
            self._listen_hold = bool(on)
            if self._listen_hold:
                self._status.mode = "listen-hold"
                self._status.message = "Listening — motors held"
                self._status.updated = time.time()
        if on:
            self._send_drive(self._cfg.esp_base, 0, 0, force=True)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            s = self._status
            locked = bool(s.person)
            mode_s = str(s.mode)
            if s.running and (
                mode_s.startswith("us-")
                or mode_s.startswith("seek-avoid")
                or mode_s.startswith("seek-back")
                or s.obstacle in ("us-stop", "us-hold", "seek-us", "us-fence")
            ):
                lock_state = f"US AVOID ({mode_s})"
            elif s.running and locked:
                lock_state = "MOTION LOCK"
            elif s.running and mode_s.startswith("seek"):
                lock_state = "SEEK"
            elif s.running:
                lock_state = "SEEK"
            else:
                lock_state = "idle"
            return {
                "ok": True,
                "running": s.running,
                "mode": s.mode,
                "nav": s.running,
                "esp": s.esp,
                "left": s.left,
                "right": s.right,
                "person": s.person,
                "lock": s.person,
                "lock_state": lock_state,
                "cx": round(s.cx, 3),
                "cy": round(s.cy, 3),
                "area": round(s.area, 4),
                "fps": round(s.fps, 1),
                "detect_ms": round(s.detect_ms, 1),
                "drive_ms": round(s.drive_ms, 1),
                "frames": s.frames,
                "message": s.message,
                "source": s.source,
                "boxes": s.boxes,
                "humans": list(s.humans),
                "target_human": s.target_human,
                "target_id": s.target_human,
                "human": s.target_human,
                "ultrasonic_cm": s.ultrasonic_cm,
                "mic_level": round(s.mic_level, 3),
                "mic_src": s.mic_src,
                "obstacle": s.obstacle,
                "sensors": s.sensors,
                "last_error": s.last_error,
                "updated": s.updated,
                "annot_seq": s.annot_seq,
                "cam": CAM_HUB.status(),
                "tunables": {
                    "forward_fast": self._cfg.forward_fast,
                    "us_stop_cm": self._cfg.us_stop_cm,
                    "us_hold_cm": self._cfg.us_hold_cm,
                    "us_seek_avoid_cm": self._cfg.us_seek_avoid_cm,
                    "area_follow_ok": self._cfg.area_follow_ok,
                    "lost_stop_s": self._cfg.lost_stop_s,
                    "lock_clear_s": self._cfg.lock_clear_s,
                    "iou_reacquire": self._cfg.iou_reacquire,
                    "iou_cross_ignore": self._cfg.iou_cross_ignore,
                    "target_human": self._target_human,
                    "locked_id": self._locked_id,
                    "detector": self._detector,
                    "vision": "motion",
                    "seek_dir": self._seek_dir,
                    "motion_energy": round(float(self._motion_energy), 2),
                    "ego_turning": self._is_ego_turning(),
                },
                "yolo": (YOLO_TRACKER.status() if YOLO_TRACKER is not None else None),
            }

    def annotated_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._annot_jpeg

    def wait_annotated(
        self, timeout: float = 1.0, after_seq: int = -1
    ) -> Tuple[Optional[bytes], int]:
        deadline = time.time() + timeout
        with self._annot_cond:
            while True:
                if self._annot_jpeg is not None and self._annot_seq > after_seq:
                    return self._annot_jpeg, self._annot_seq
                remaining = deadline - time.time()
                if remaining <= 0:
                    return self._annot_jpeg, self._annot_seq
                self._annot_cond.wait(timeout=remaining)

    def set_target_human(self, n: int) -> Dict[str, Any]:
        n = int(max(1, min(9, n)))
        with self._lock:
            self._target_human = n
            self._cfg.target_human = n
            self._locked_id = None
            self._locked_box = None
            self._smooth_box = None
            self._status.target_human = n
            self._status.message = f"Target -> Human {n} (motion re-lock)"
            self._status.updated = time.time()
        return {"ok": True, "target_human": n, "target_id": n, **self.status()}

    def set_target(self, target_id: Optional[int]) -> Dict[str, Any]:
        if target_id is None:
            return {"ok": True, "target_human": self._target_human, **self.status()}
        return self.set_target_human(int(target_id))

    def start(self, esp_base: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            if "target_id" in kwargs and kwargs.get("target_human") is None:
                kwargs["target_human"] = kwargs.get("target_id")
            if "target_human" in kwargs and kwargs["target_human"] is not None:
                try:
                    self._target_human = int(kwargs["target_human"])
                except Exception:
                    pass
            if self._thread and self._thread.is_alive():
                if "target_human" in kwargs and kwargs["target_human"] is not None:
                    return self.set_target_human(int(kwargs["target_human"]))
                return {"ok": True, "already": True, **self.status()}
            cfg = FollowConfig(
                esp_base=(esp_base or self._cfg.esp_base).rstrip("/"),
                target_human=self._target_human,
            )
            for k, v in kwargs.items():
                if v is None or not hasattr(cfg, k):
                    continue
                try:
                    typ = type(getattr(cfg, k))
                    if typ is bool:
                        setattr(cfg, k, str(v).lower() in ("1", "true", "yes", "on"))
                    else:
                        setattr(cfg, k, typ(v))
                except Exception:
                    pass
            self._target_human = int(cfg.target_human)
            self._cfg = cfg
            self._stop.clear()
            self._prev_gray = None
            self._tracks = {}
            self._locked_id = None
            self._locked_box = None
            self._smooth_box = None
            self._acquire_hits = 0
            self._acquire_box = None
            self._lock_miss = 0
            self._stuck_since = None
            self._reverse_until = 0.0
            self._seek_dir = 1
            self._seek_flip_at = time.time() + cfg.seek_flip_s
            self._seek_backoff_until = 0.0
            self._seek_look_until = 0.0
            self._seek_look_next = time.time() + 1.5
            self._motion_energy = 0.0
            self._mog2 = None
            self._detector = "motion"
            self._last_humans = []
            # Warm YOLO in background if present — bonus only
            if YOLO_TRACKER is not None:
                try:
                    YOLO_TRACKER.ensure()
                except Exception:
                    pass
            self._status = FollowStatus(
                running=True,
                mode="seek-scan",
                esp=cfg.esp_base,
                target_human=self._target_human,
                message=f"Nav ON — SEEK for motion -> H{self._target_human}",
                updated=time.time(),
            )
            CAM_HUB.ensure(cfg.esp_base)
            self._sensors.start(cfg.esp_base)
            self._thread = threading.Thread(
                target=self._run, name="trace-e-follow", daemon=True
            )
            self._thread.start()
            return {"ok": True, "started": True, **self.status()}

    def stop(self, reason: str = "stopped") -> Dict[str, Any]:
        self._stop.set()
        self._sensors.stop()
        thr = None
        with self._lock:
            thr = self._thread
            esp = self._cfg.esp_base
        if thr and thr.is_alive():
            thr.join(timeout=2.5)
        self._send_drive(esp, 0, 0, force=True)
        with self._lock:
            self._status.running = False
            self._status.left = 0
            self._status.right = 0
            self._status.mode = "idle"
            self._status.message = reason
            self._status.person = False
            self._status.boxes = 0
            self._status.humans = []
            self._status.obstacle = ""
            self._status.source = ""
            self._locked_id = None
            self._locked_box = None
            self._smooth_box = None
            self._acquire_hits = 0
            self._acquire_box = None
            self._lock_miss = 0
            self._status.updated = time.time()
            self._thread = None
            return {"ok": True, "stopped": True, **self.status()}

    def _set_status(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._status, k):
                    setattr(self._status, k, v)
            self._status.updated = time.time()

    def _publish_annot(self, jpg: bytes) -> None:
        with self._annot_cond:
            self._annot_jpeg = jpg
            self._annot_seq += 1
            self._status.annot_seq = self._annot_seq
            self._annot_cond.notify_all()

    def _ensure_drive_worker(self) -> None:
        if self._drive_thread and self._drive_thread.is_alive():
            return
        self._drive_stop.clear()
        self._drive_thread = threading.Thread(
            target=self._drive_worker, name="trace-drive", daemon=True
        )
        self._drive_thread.start()

    def _drive_worker(self) -> None:
        last_sent = (None, None)  # type: ignore
        last_t = 0.0
        while not self._drive_stop.is_set():
            self._drive_wake.wait(timeout=0.35)
            self._drive_wake.clear()
            with self._lock:
                cmd = self._drive_cmd
                esp = self._drive_esp
                force = self._drive_force.is_set()
                if force:
                    self._drive_force.clear()
            now = time.time()
            if (
                not force
                and cmd == last_sent
                and (cmd == (0, 0) or (now - last_t) < 0.28)
            ):
                continue
            url = _drive_url(esp, cmd[0], cmd[1])
            t0 = time.perf_counter()
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=0.9) as resp:
                    resp.read(64)
                last_sent = cmd
                last_t = time.time()
                self._last_drive = cmd
                with self._lock:
                    if str(self._status.last_error).startswith("drive:"):
                        self._status.last_error = ""
                    self._status.drive_ms = (time.perf_counter() - t0) * 1000.0
            except Exception as exc:
                self._set_status(last_error=f"drive: {exc}")

    def _send_drive(self, esp_base: str, left: int, right: int, force: bool = False) -> float:
        self._ensure_drive_worker()
        with self._lock:
            self._drive_esp = esp_base.rstrip("/")
            self._drive_cmd = (int(left), int(right))
            if force:
                self._drive_force.set()
        self._drive_wake.set()
        return 0.0

    def _ensure_mog2(self):
        if self._mog2 is None:
            self._mog2 = cv2.createBackgroundSubtractorMOG2(
                history=60, varThreshold=36, detectShadows=False
            )
        return self._mog2

    def _is_ego_turning(self) -> bool:
        """True while actively yawing — NOT while motors are stopped (seek-look)."""
        L, R = self._last_drive
        if L == 0 and R == 0:
            return False
        if L * R < 0:
            return True
        if abs(L - R) >= 16 and abs(L + R) < 36:
            return True
        return False

    def _merge_motion_blobs(
        self,
        scored: List[Tuple[Tuple[int, int, int, int], float]],
        w: int,
        h: int,
    ) -> List[Tuple[Tuple[int, int, int, int], float]]:
        """Aggressively merge overlapping / nearby motion boxes into coherent blobs."""
        if not scored:
            return []
        diag = float((w * w + h * h) ** 0.5)
        merge_dist = MOTION_MERGE_DIST_FRAC * diag
        items = sorted(
            scored,
            key=lambda t: t[0][2] * t[0][3] * (1.0 + t[1] * 0.04),
            reverse=True,
        )
        used = [False] * len(items)
        merged: List[Tuple[Tuple[int, int, int, int], float]] = []
        for i, (box, mot) in enumerate(items):
            if used[i]:
                continue
            x, y, bw, bh = box
            x2, y2 = x + bw, y + bh
            best_mot = mot
            used[i] = True
            changed = True
            while changed:
                changed = False
                for j, (ob, om) in enumerate(items):
                    if used[j]:
                        continue
                    ox, oy, ow, oh = ob
                    cand = (x, y, x2 - x, y2 - y)
                    if (
                        _iou(cand, ob) >= MOTION_MERGE_IOU
                        or _centroid_dist(cand, ob) < merge_dist
                    ):
                        used[j] = True
                        changed = True
                        x = min(x, ox)
                        y = min(y, oy)
                        x2 = max(x2, ox + ow)
                        y2 = max(y2, oy + oh)
                        best_mot = max(best_mot, om)
            nbw, nbh = max(1, x2 - x), max(1, y2 - y)
            merged.append(((x, y, nbw, nbh), best_mot))
        return merged

    def _detect_motion_boxes(
        self, gray: np.ndarray, w: int, h: int, *, ego: bool = False
    ) -> List[Tuple[Tuple[int, int, int, int], float]]:
        """Significant moving blobs via absdiff (+ optional MOG2 when still).

        Caps to ≤2 coherent movers. Suppresses room-wide ego flood while pivoting.
        """
        if self._prev_gray is None or gray.shape != self._prev_gray.shape:
            return []
        diff = cv2.absdiff(gray, self._prev_gray)
        blur = cv2.GaussianBlur(diff, (5, 5), 0)
        global_e = float(np.mean(blur))
        self._motion_energy = global_e
        thresh = MOTION_DIFF_THRESH
        if ego or global_e >= MOTION_EGO_GLOBAL:
            thresh = MOTION_DIFF_THRESH + MOTION_EGO_DIFF_BOOST
            # Subtract global ego: keep only pixels clearly above scene motion
            thr_dyn = max(thresh, int(global_e * 2.2 + 8))
            thresh = thr_dyn
        _, mask = cv2.threshold(blur, thresh, 255, cv2.THRESH_BINARY)
        # No MOG2 — it was OR'd before and fake-painted furniture as H1 forever.
        k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k_open)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_close)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_area = float(max(1, w * h))
        min_a = MOTION_MIN_AREA * frame_area
        max_a = MOTION_MAX_AREA * frame_area
        # Tighter while ego-flooding: no giant boxes, higher energy floor
        if ego or global_e >= MOTION_EGO_GLOBAL:
            max_a = min(max_a, 0.22 * frame_area)
            mot_floor = MOTION_ACQUIRE_MIN + global_e * 0.45
        else:
            mot_floor = MOTION_ACQUIRE_MIN
        scored: List[Tuple[Tuple[int, int, int, int], float]] = []
        for c in contours:
            a = float(cv2.contourArea(c))
            if a < min_a or a > max_a:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            if bw < MOTION_MIN_SIDE or bh < MOTION_MIN_SIDE:
                continue
            box_frac = (bw * bh) / frame_area
            if box_frac > MOTION_MAX_AREA or box_frac > (max_a / frame_area):
                continue
            fill = a / float(max(1, bw * bh))
            if fill < MOTION_MIN_FILL:
                continue  # static edges / hollow noise
            aspect = max(bw, bh) / float(max(1, min(bw, bh)))
            if aspect > MOTION_MAX_ASPECT:
                continue
            cy = (y + bh * 0.5) / float(max(1, h))
            # Floor cam (~13–25 cm): movers are legs — reject ceiling / upper junk
            if cy < MOTION_CY_MIN and box_frac < 0.12:
                continue
            # Light pad only — avoid swallowing the room
            pad = int(0.03 * max(bw, bh))
            x = max(0, x - pad)
            y = max(0, y - pad)
            bw = min(w - x, bw + 2 * pad)
            bh = min(h - y, bh + 2 * pad)
            roi = blur[y : y + bh, x : x + bw]
            motion = float(np.mean(roi)) if roi.size else 0.0
            # Local must clearly beat global ego
            if motion < mot_floor or motion < (global_e * 1.55 + 3.0):
                continue
            # Prefer lower-frame movers (legs) via mild score boost later
            scored.append(((x, y, bw, bh), motion * (1.0 + max(0.0, cy - 0.35) * 0.35)))

        scored = self._merge_motion_blobs(scored, w, h)
        # Drop merged giants that ate the frame
        filtered: List[Tuple[Tuple[int, int, int, int], float]] = []
        for box, mot in scored:
            bf = (box[2] * box[3]) / frame_area
            if bf > MOTION_MAX_AREA:
                continue
            if ego and bf > 0.24:
                continue
            filtered.append((box, mot))
        filtered.sort(
            key=lambda t: (t[0][2] * t[0][3] * (1.0 + t[1] * 0.05)), reverse=True
        )

        # Cap: strongest = H1; H2 only if clearly separate solid mover
        keep: List[Tuple[Tuple[int, int, int, int], float]] = []
        if not filtered:
            return keep
        keep.append(filtered[0])
        max_keep = 1 if (ego or global_e >= MOTION_EGO_GLOBAL * 1.4) else MOTION_MAX_BLOBS
        if max_keep >= 2 and len(filtered) > 1:
            h1 = filtered[0][0]
            h1_area = float(h1[2] * h1[3])
            diag = float((w * w + h * h) ** 0.5)
            min_sep = MOTION_H2_MIN_SEP_FRAC * diag
            for box, mot in filtered[1:]:
                if _iou(box, h1) >= MOTION_MERGE_IOU:
                    continue
                if _centroid_dist(box, h1) < min_sep:
                    continue
                if (box[2] * box[3]) < h1_area * MOTION_H2_MIN_AREA_RATIO:
                    continue
                keep.append((box, mot))
                break
        return keep[:MOTION_MAX_BLOBS]

    def _yolo_bonus(
        self, frame: np.ndarray
    ) -> List[Tuple[Tuple[int, int, int, int], float]]:
        """Optional YOLO person boxes — never required for lock/follow."""
        if YOLO_TRACKER is None:
            return []
        try:
            if not YOLO_TRACKER.ready:
                return []
            dets = YOLO_TRACKER.track(frame)
            out = []
            for d in dets:
                out.append((tuple(d["box"]), float(d.get("conf", 0.3))))
            return out
        except Exception:
            return []

    def _nms(
        self, boxes: List[Tuple[int, int, int, int]], thresh: float = 0.35
    ) -> List[Tuple[int, int, int, int]]:
        if not boxes:
            return []
        boxes = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)
        keep: List[Tuple[int, int, int, int]] = []
        for b in boxes:
            if all(_iou(b, k) < thresh for k in keep):
                keep.append(b)
        return keep

    def _assign_humans(
        self,
        motion_scored: List[Tuple[Tuple[int, int, int, int], float]],
        yolo_boxes: List[Tuple[Tuple[int, int, int, int], float]],
        w: int,
        h: int,
    ) -> List[Dict[str, Any]]:
        """
        Motion blobs are humans (max 2). YOLO may refine a box only.
        H1 = strongest coherent mover; H2 only if clearly separate.
        Once locked, prefer sticky track — don't spawn a forest.
        """
        refined: List[Tuple[Tuple[int, int, int, int], float, bool]] = []
        for box, mot in motion_scored:
            confirmed = False
            best = box
            for yb, conf in yolo_boxes:
                if _iou(box, yb) >= 0.15:
                    best = _smooth_box(box, yb, 0.35)
                    confirmed = True
                    mot = max(mot, conf * 10.0)
                    break
            refined.append((best, mot, confirmed))

        # Stationary look / empty motion: YOLO alone can create H1 (kicking leg / person)
        if not refined and yolo_boxes:
            for yb, conf in sorted(yolo_boxes, key=lambda t: t[1], reverse=True)[:MOTION_MAX_BLOBS]:
                bf = (yb[2] * yb[3]) / float(max(1, w * h))
                if bf < 0.008 or bf > MOTION_MAX_AREA:
                    continue
                refined.append((yb, max(8.0, conf * 20.0), True))

        # Strength-first before track assign (H1 = person, not leftmost speck)
        refined.sort(
            key=lambda t: (t[0][2] * t[0][3] * (1.0 + t[1] * 0.05)), reverse=True
        )
        refined = refined[:MOTION_MAX_BLOBS]

        # Sticky lock: keep locked blob only if still overlapping with real motion
        if self._locked_box is not None and refined:
            locked_match = None
            others: List[Tuple[Tuple[int, int, int, int], float, bool]] = []
            for box, mot, conf_y in refined:
                iou = _iou(self._locked_box, box)
                cd = _centroid_dist(self._locked_box, box)
                if iou >= 0.12 or (iou >= 0.05 and cd < 70 and mot >= MOTION_LOCK_MIN):
                    if locked_match is None or mot > locked_match[1]:
                        locked_match = (box, mot, conf_y)
                else:
                    others.append((box, mot, conf_y))
            kept: List[Tuple[Tuple[int, int, int, int], float, bool]] = []
            if locked_match is not None:
                kept.append(locked_match)
                diag = float((w * w + h * h) ** 0.5)
                for box, mot, conf_y in others:
                    if _centroid_dist(locked_match[0], box) < MOTION_H2_MIN_SEP_FRAC * diag:
                        continue
                    if (box[2] * box[3]) < (
                        locked_match[0][2] * locked_match[0][3]
                    ) * MOTION_H2_MIN_AREA_RATIO:
                        continue
                    kept.append((box, mot, conf_y))
                    break
            else:
                kept = refined[:MOTION_MAX_BLOBS]
            refined = kept

        used_prev = set()
        assigned: Dict[int, Tuple[int, int, int, int]] = {}
        meta: Dict[int, Dict[str, Any]] = {}
        unmatched: List[Tuple[Tuple[int, int, int, int], float, bool]] = []
        for box, mot, conf_y in refined:
            best_id, best_sc = None, 0.12
            for hid, pb in self._tracks.items():
                if hid in used_prev:
                    continue
                sc = _iou(box, pb)
                cd = _centroid_dist(box, pb)
                diag = float((w * w + h * h) ** 0.5)
                near = 1.0 - min(1.0, cd / max(40.0, 0.18 * diag))
                score = sc + 0.25 * near
                if score > best_sc:
                    best_sc, best_id = score, hid
            if best_id is not None:
                assigned[best_id] = box
                used_prev.add(best_id)
                meta[best_id] = {"motion": mot, "yolo": conf_y}
            else:
                unmatched.append((box, mot, conf_y))
        next_id = 1
        for box, mot, conf_y in unmatched:
            while next_id in assigned:
                next_id += 1
            assigned[next_id] = box
            meta[next_id] = {"motion": mot, "yolo": conf_y}
            next_id += 1

        self._tracks = dict(assigned)
        # H1 = strongest (area×motion); H2 = remaining (if any)
        ordered = sorted(
            assigned.items(),
            key=lambda kv: (
                kv[1][2]
                * kv[1][3]
                * (1.0 + float(meta.get(kv[0], {}).get("motion", 0.0)) * 0.05)
            ),
            reverse=True,
        )[:MOTION_MAX_BLOBS]
        humans: List[Dict[str, Any]] = []
        for slot, (tid, box) in enumerate(ordered, start=1):
            x, y, bw, bh = box
            m = meta.get(tid, {})
            humans.append(
                {
                    "id": slot,
                    "track_id": tid,
                    "label": f"H{slot}",
                    "box": box,
                    "cx": (x + bw * 0.5) / float(max(1, w)),
                    "cy": (y + bh * 0.5) / float(max(1, h)),
                    "area": (bw * bh) / float(max(1, w * h)),
                    "motion": round(float(m.get("motion", 0.0)), 2),
                    "yolo": bool(m.get("yolo")),
                    "conf": round(float(m.get("motion", 0.0)) / 30.0, 2),
                }
            )
        return humans

    def _resolve_locked(
        self, humans: List[Dict[str, Any]], cfg: FollowConfig
    ) -> Optional[Dict[str, Any]]:
        """Sticky: stay on locked blob via track_id / IoU — need real overlap."""
        if not humans:
            return None
        if self._locked_id is not None:
            by_tid = next(
                (h for h in humans if int(h.get("track_id", -1)) == self._locked_id),
                None,
            )
            if by_tid is not None:
                mot = float(by_tid.get("motion") or 0.0)
                if mot >= MOTION_LOCK_MIN * 0.55 or bool(by_tid.get("yolo")):
                    return by_tid
        if self._locked_box is None:
            return None
        matches: List[Tuple[float, Dict[str, Any]]] = []
        for h in humans:
            iou = _iou(self._locked_box, h["box"])
            mot = float(h.get("motion") or 0.0)
            if iou < 0.10:
                continue
            if mot < MOTION_LOCK_MIN * 0.45 and not h.get("yolo"):
                continue
            matches.append((iou * (1.0 + mot * 0.02), h))
        if not matches:
            return None
        matches.sort(key=lambda t: t[0], reverse=True)
        best = matches[0][1]
        self._locked_id = int(best.get("track_id", best["id"]))
        return best

    def _acquire(
        self,
        humans: List[Dict[str, Any]],
        target_id: int,
        cfg: FollowConfig,
    ) -> Optional[Dict[str, Any]]:
        """Need sustained motion hits before locking — no instant fake H1."""
        if not humans:
            self._acquire_hits = 0
            self._acquire_box = None
            return None
        # Prefer requested slot, else strongest mover with lower-frame bias
        cand = next((h for h in humans if int(h["id"]) == int(target_id)), None)
        if cand is None:
            cand = next((h for h in humans if int(h["id"]) == 1), None)
        if cand is None:
            ranked = sorted(
                humans,
                key=lambda h: (
                    float(h.get("area", 0.0))
                    * (1.0 + float(h.get("motion", 0.0)) * 0.05)
                    * (1.0 + max(0.0, float(h.get("cy", 0.5)) - 0.3) * 0.4)
                ),
                reverse=True,
            )
            cand = ranked[0]
        mot = float(cand.get("motion") or 0.0)
        yolo_ok = bool(cand.get("yolo"))
        if mot < cfg.motion_acquire_min and not yolo_ok:
            self._acquire_hits = 0
            self._acquire_box = None
            return None
        box = tuple(cand["box"])
        if self._acquire_box is not None and (
            _iou(self._acquire_box, box) >= 0.12
            or _centroid_dist(self._acquire_box, box) < 80
        ):
            self._acquire_hits += 1
            self._acquire_box = _smooth_box(self._acquire_box, box, 0.45)
        else:
            self._acquire_hits = 1
            self._acquire_box = box
        need = 2 if yolo_ok else MOTION_HIT_FRAMES
        if self._acquire_hits < need:
            return None
        self._acquire_hits = 0
        self._acquire_box = None
        return cand

    def _annotate(
        self,
        frame: np.ndarray,
        humans: List[Dict[str, Any]],
        target_id: int,
        left: int,
        right: int,
        mode: str,
        person: bool,
        source: str,
        mic_level: float,
        us_cm: Optional[float],
        obstacle: str,
        cfg: FollowConfig,
    ) -> bytes:
        vis = frame.copy()
        h, w = vis.shape[:2]
        scale = 640.0 / max(w, 1)
        if scale < 1.0:
            vis = cv2.resize(vis, (int(w * scale), int(h * scale)))
            sx = scale
        else:
            sx = 1.0
        hh, ww = vis.shape[:2]
        cv2.line(vis, (ww // 2, 0), (ww // 2, hh), (0, 220, 255), 2)

        for hum in humans:
            box = hum["box"]
            hid = int(hum["id"])
            tid = int(hum.get("track_id", hid))
            x, y, bw, bh = [int(v * sx) for v in box]
            is_tgt = person and (
                tid == self._locked_id or (self._locked_id is None and hid == target_id)
            )
            color = (0, 255, 80) if is_tgt else (80, 200, 255)
            thick = 4 if is_tgt else 2
            cv2.rectangle(vis, (x, y), (x + bw, y + bh), color, thick)
            tag = f"H{hid}" + (" *" if is_tgt else "")
            if hum.get("yolo"):
                tag += " Y"
            cv2.rectangle(vis, (x, max(0, y - 22)), (x + max(54, 8 * len(tag)), y), color, -1)
            cv2.putText(
                vis, tag, (x + 4, max(14, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2
            )
            if is_tgt:
                cv2.drawMarker(
                    vis,
                    (x + bw // 2, y + bh // 2),
                    (0, 255, 255),
                    markerType=cv2.MARKER_STAR,
                    markerSize=28,
                    thickness=2,
                )

        seeking = (not person) and mode.startswith("seek")
        us_alarm = bool(
            mode.startswith("us-")
            or mode.startswith("seek-avoid")
            or mode.startswith("seek-back")
            or obstacle in ("us-stop", "us-hold", "seek-us", "us-fence")
        )
        if us_alarm:
            lock = f"US BUMPER  {mode.upper()}"
            cv2.rectangle(vis, (0, 0), (ww, 56), (0, 0, 200), -1)
        elif person:
            lock = f"H{target_id} MOTION LOCK"
            cv2.rectangle(vis, (0, 0), (ww, 56), (0, 90, 40), -1)
        elif seeking:
            lock = "SEEK / SCAN"
            cv2.rectangle(vis, (0, 0), (ww, 56), (40, 70, 140), -1)
        else:
            lock = f"H{target_id} LOST"
            cv2.rectangle(vis, (0, 0), (ww, 56), (0, 0, 160), -1)
        hud = f"{mode.upper()}  {lock}  {source}  L{left} R{right}"
        cv2.putText(vis, hud, (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2)

        us_txt = f"US {us_cm:.0f}cm" if us_cm is not None else "US —"
        if us_cm is not None and us_cm <= cfg.us_stop_cm:
            us_txt = f"US STOP {us_cm:.0f}cm"
        elif us_cm is not None and us_cm <= cfg.us_hold_cm:
            us_txt = f"US HOLD {us_cm:.0f}cm"
        foot = f"{us_txt}  mic {mic_level:.2f}"
        if obstacle:
            foot += f"  !{obstacle}"
        cv2.rectangle(vis, (0, hh - 32), (ww, hh), (0, 0, 0), -1)
        cv2.putText(
            vis,
            foot,
            (10, hh - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 80, 255) if us_alarm else ((0, 255, 180) if not obstacle and person else (0, 180, 255)),
            2,
        )

        ok, buf = cv2.imencode(".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
        return bytes(buf) if ok else b""

    def _run(self) -> None:
        cfg = self._cfg
        CAM_HUB.ensure(cfg.esp_base)
        last_seen = 0.0
        last_source = ""
        humans: List[Dict[str, Any]] = []
        frame_i = 0
        fps_t0 = time.perf_counter()
        fps_n = 0
        period = 1.0 / max(4.0, cfg.drive_hz)
        seq = -1
        self._set_status(
            message=f"SEEK — scan for motion -> follow H{self._target_human}",
            mode="seek-scan",
            target_human=self._target_human,
        )

        while not self._stop.is_set():
            loop_t = time.perf_counter()
            target_id = self._target_human
            jpg, seq = CAM_HUB.wait_jpeg(timeout=0.8, after_seq=seq)
            if jpg is None:
                self._send_drive(cfg.esp_base, 0, 0)
                self._set_status(mode="no-cam", message="No cam — motors stopped", person=False)
                continue

            frame = CAM_HUB.latest_bgr()
            if frame is None:
                arr = np.frombuffer(jpg, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                continue

            frame_mean = float(np.mean(frame))
            if frame_mean < 18.0:
                # Still seek-turn slowly? Safer stop — can't see
                self._send_drive(cfg.esp_base, 0, 0)
                annot = self._annotate(
                    frame, [], self._target_human, 0, 0, "cam-dark", False,
                    "dark", 0.0, None, "cam-dark", cfg,
                )
                if annot:
                    self._publish_annot(annot)
                self._set_status(
                    running=True, mode="cam-dark", left=0, right=0, person=False,
                    boxes=0, humans=[], message="Cam too dark — uncover lens",
                    source="dark", obstacle="cam-dark",
                )
                time.sleep(0.15)
                continue

            h, w = frame.shape[:2]
            frame_i += 1
            fps_n += 1
            detect_ms = 0.0
            source = "motion"
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            now_pre = time.time()
            looking = now_pre < self._seek_look_until
            # Ego only while pivoting — NEVER during seek-look (motors 0) or we'd miss a kicking leg
            if self._is_ego_turning():
                self._ego_until = now_pre + 0.35
            ego = (not looking) and (
                self._is_ego_turning() or now_pre < self._ego_until
            )

            if frame_i % max(1, cfg.detect_every) == 0:
                t0 = time.perf_counter()
                motion_scored = self._detect_motion_boxes(gray, w, h, ego=ego)
                yolo_boxes: List[Tuple[Tuple[int, int, int, int], float]] = []
                # While stopped looking, YOLO is a real acquire path (bonus otherwise)
                if YOLO_TRACKER is not None and YOLO_TRACKER.ready:
                    yolo_boxes = self._yolo_bonus(frame)
                humans = self._assign_humans(motion_scored, yolo_boxes, w, h)
                # If motion missed but YOLO sees a person while we're still — take it
                if not humans and looking and yolo_boxes:
                    humans = self._assign_humans([], yolo_boxes, w, h)
                    for hh in humans:
                        hh["motion"] = max(float(hh.get("motion") or 0.0), cfg.motion_acquire_min)
                n_y = sum(1 for hh in humans if hh.get("yolo"))
                source = f"motion+yolo" if n_y else "motion"
                if ego and not humans:
                    source = "motion-ego"
                if looking and not ego:
                    source = source + "+look"
                detect_ms = (time.perf_counter() - t0) * 1000.0
                self._detector = source
                self._last_humans = humans
            else:
                humans = list(getattr(self, "_last_humans", []) or [])

            # Sticky lock vs fresh acquire — drop lock if motion dies (fake H1)
            chosen = None
            if self._locked_box is not None or self._locked_id is not None:
                chosen = self._resolve_locked(humans, cfg)
                if chosen is None:
                    self._lock_miss += 1
                    if self._lock_miss >= MOTION_MISS_DROP:
                        self._locked_id = None
                        self._locked_box = None
                        self._smooth_box = None
                        self._lock_miss = 0
                        self._acquire_hits = 0
                        self._acquire_box = None
                else:
                    self._lock_miss = 0
            else:
                chosen = self._acquire(humans, target_id, cfg)

            if chosen is not None:
                self._locked_id = int(chosen.get("track_id", chosen["id"]))
                self._smooth_box = _smooth_box(
                    self._smooth_box, chosen["box"], cfg.box_smooth
                )
                sb = self._smooth_box
                x, y, bw, bh = sb
                chosen = dict(chosen)
                chosen["box"] = sb
                chosen["cx"] = (x + bw * 0.5) / float(max(1, w))
                chosen["cy"] = (y + bh * 0.5) / float(max(1, h))
                chosen["area"] = (bw * bh) / float(max(1, w * h))
                self._locked_box = sb
                # Keep real H1/H2 slot labels — don't rewrite everything to H1
                cleaned: List[Dict[str, Any]] = []
                for hman in humans:
                    hman = dict(hman)
                    if int(hman.get("track_id", -1)) == self._locked_id:
                        hman["box"] = sb
                        hman["active"] = True
                        cleaned.insert(0, hman)
                        target_id = int(hman.get("id", target_id))
                    elif len(cleaned) < 2:
                        hman["active"] = False
                        cleaned.append(hman)
                humans = cleaned[:2] if cleaned else humans[:1]

            left = right = 0
            mode = "seek-scan"
            person = False
            cx = cy = 0.5
            area = 0.0
            obstacle = ""

            snap = self._sensors.snapshot()
            us_cm = snap.get("ultrasonic_cm")
            try:
                us_cm = float(us_cm) if us_cm is not None else None
            except (TypeError, ValueError):
                us_cm = None
            # Glitch floor only: treat sub-noise as CONTACT (hard stop), never drop US
            if us_cm is not None:
                if us_cm < cfg.us_noise_cm:
                    us_cm = 0.5  # contact / invalid-low -> treat as bumper hit
                elif us_cm > cfg.us_max_cm:
                    us_cm = None
            mic_level = float(snap.get("mic_level") or 0.0)

            miss_age = time.time() - last_seen if last_seen > 0 else 1e9
            now = time.time()

            # Sudden US drop while moving -> immediate reverse burst
            if (
                us_cm is not None
                and self._us_prev is not None
                and self._us_prev > cfg.us_hold_cm
                and us_cm <= cfg.us_stop_cm
            ):
                self._us_reverse_until = max(self._us_reverse_until, now + US_REVERSE_S)
            self._us_prev = us_cm

            if chosen is not None:
                last_seen = now
                last_source = source
                cx = float(chosen["cx"])
                cy = float(chosen["cy"])
                area = float(chosen["area"])
                person = True
                # Display Hn of locked track (L->R label)
                target_id = int(chosen.get("id", target_id))
                left, right, mode = follow_behind_drive(cx, area, cfg, us_cm)
                if mode == "us-stop":
                    self._us_reverse_until = max(self._us_reverse_until, now + US_REVERSE_S)
                    obstacle = "us-stop"
                elif mode.startswith("us-hold"):
                    obstacle = "us-hold"
                if not any(int(h.get("track_id", -1)) == self._locked_id for h in humans):
                    humans = list(humans) + [
                        {
                            "id": target_id,
                            "track_id": self._locked_id,
                            "label": f"H{target_id}",
                            "box": chosen["box"],
                            "cx": cx,
                            "cy": cy,
                            "area": area,
                            "motion": chosen.get("motion"),
                            "yolo": chosen.get("yolo"),
                        }
                    ]
            elif miss_age <= min(1.0, cfg.lost_stop_s) and self._locked_box is not None:
                # Very short grace — show last box but do NOT drive on a ghost lock
                x, y, bw, bh = self._locked_box
                cx = (x + bw * 0.5) / float(w)
                area = (bw * bh) / float(max(1, w * h))
                person = True
                left = right = 0
                mode = "grace-hold"
                obstacle = "grace"
                source = last_source or "grace"
            else:
                # ACTIVE SEEK — never freeze facing a wall; never forward into US
                if miss_age > cfg.lock_clear_s:
                    self._locked_id = None
                    self._locked_box = None
                    self._smooth_box = None
                    self._acquire_hits = 0
                    self._acquire_box = None
                    self._lock_miss = 0
                person = False
                # Periodic scan direction flip
                if now >= self._seek_flip_at:
                    self._seek_dir *= -1
                    self._seek_flip_at = now + cfg.seek_flip_s
                # Pause pivot so absdiff sees the dancer — reset baseline or first frame is ego junk
                if now >= self._seek_look_next and now >= self._seek_look_until:
                    self._seek_look_until = now + SEEK_LOOK_S
                    self._seek_look_next = now + SEEK_LOOK_S + SEEK_LOOK_GAP_S
                    self._prev_gray = None  # clean slate for kicking-leg acquire
                    self._ego_until = 0.0
                if now < self._seek_look_until:
                    left, right, mode, flip = 0, 0, "seek-look", False
                else:
                    left, right, mode, flip = seek_scan_drive(
                        cfg, us_cm, self._seek_dir, self._seek_backoff_until, now
                    )
                if flip:
                    self._seek_dir *= -1
                    self._seek_backoff_until = now + SEEK_BACK_S
                    self._seek_flip_at = now + cfg.seek_flip_s
                    obstacle = "seek-us"
                elif mode == "seek-back":
                    obstacle = "seek-back"
                elif mode.startswith("seek-avoid"):
                    obstacle = "seek-us"
                elif mode == "seek-look":
                    obstacle = ""
                source = source or "seek"

            self._prev_gray = gray

            if person and cfg.use_esp_mic and mic_level >= cfg.mic_stop_level:
                # Loud burst near Trace while locked — soft stop (close talk / shout)
                if us_cm is None or us_cm <= cfg.us_hold_cm + 25:
                    left, right = 0, 0
                    mode = "mic-soft-stop"
                    obstacle = "mic-soft-stop"
            if (
                (not person)
                and cfg.use_esp_mic
                and mic_level >= cfg.mic_presence_level
                and str(mode).startswith("seek")
            ):
                # Cam + mic: noise nearby while seeking -> freeze and look
                left, right = 0, 0
                mode = "seek-listen"
                self._seek_look_until = max(self._seek_look_until, now + 0.85)
                obstacle = obstacle or "mic-presence"

            forwarding = (left + right) > 24 and left > 0 and right > 0
            if forwarding and person:
                if self._stuck_since is None:
                    self._stuck_since = now
                elif (now - self._stuck_since) >= STUCK_S:
                    self._reverse_until = now + REVERSE_S
                    self._stuck_since = None
                    obstacle = "anti-burnout"
            else:
                self._stuck_since = None

            if now < self._reverse_until:
                left = right = -REVERSE_PWM
                mode = "reverse"
                obstacle = obstacle or "anti-burnout"

            # === HC-SR04 HARD FENCE (wins over follow/seek/grace) ===
            # Except listen-hold: covering US to talk is intentional — don't reverse-fight the hand
            us_force = False
            if self._listen_hold:
                left, right = 0, 0
                mode = "listen-hold"
                obstacle = "listening"
                us_force = True
            elif us_cm is not None and us_cm <= cfg.us_stop_cm:
                left = right = -US_REVERSE_PWM
                mode = "us-stop"
                obstacle = "us-stop"
                self._us_reverse_until = max(self._us_reverse_until, now + US_REVERSE_S)
                us_force = True
            elif now < self._us_reverse_until:
                left = right = -US_REVERSE_PWM
                mode = "us-stop"
                obstacle = "us-stop"
                us_force = True
            elif us_cm is not None and us_cm <= cfg.us_hold_cm:
                # No forward past hold fence — allow in-place yaw only
                if left > 0 or right > 0:
                    # kill forward component; keep differential turn if any
                    fwd = (left + right) * 0.5
                    if fwd > 0:
                        left = clamp_motor(left - fwd)
                        right = clamp_motor(right - fwd)
                    mode = "us-hold" if mode.startswith("us-") else "us-hold"
                    obstacle = "us-hold"
                    us_force = True
                if left > 0 or right > 0:
                    # still any forward wheel? hard zero
                    if left > 0 and right > 0:
                        left = right = 0
                        mode = "us-hold"
                        obstacle = "us-hold"
                        us_force = True

            drive_ms = self._send_drive(cfg.esp_base, left, right, force=us_force)
            hud_target = int(chosen["id"]) if (person and chosen is not None) else int(
                self._target_human
            )
            if person and chosen is None and self._locked_id is not None:
                for hman in humans:
                    if int(hman.get("track_id", -1)) == self._locked_id:
                        hud_target = int(hman["id"])
                        break
                else:
                    hud_target = int(self._target_human)
            annot = self._annotate(
                frame, humans, hud_target, left, right, mode, person,
                source or "-", mic_level, us_cm, obstacle, cfg,
            )
            if annot:
                self._publish_annot(annot)

            nowp = time.perf_counter()
            if nowp - fps_t0 >= 1.0:
                fps = fps_n / (nowp - fps_t0)
                fps_t0 = nowp
                fps_n = 0
            else:
                fps = self._status.fps

            if person:
                msg = f"H{hud_target} {mode} · L{left} R{right}"
            else:
                msg = f"SEEK {mode} · L{left} R{right}"
            if us_cm is not None:
                if us_cm <= cfg.us_stop_cm:
                    msg = f"US-STOP {us_cm:.0f}cm · {mode} · L{left} R{right}"
                elif us_cm <= cfg.us_hold_cm:
                    msg = f"US-HOLD {us_cm:.0f}cm · {mode} · L{left} R{right}"
                else:
                    msg += f" · {us_cm:.0f}cm"
            if obstacle:
                msg += f" · !{obstacle}"

            self._set_status(
                running=True,
                mode=mode,
                esp=cfg.esp_base,
                left=left,
                right=right,
                person=person,
                cx=cx,
                cy=cy,
                area=area,
                fps=fps,
                detect_ms=detect_ms,
                drive_ms=drive_ms,
                frames=frame_i,
                source=source,
                boxes=len(humans),
                humans=[
                    {
                        "id": x["id"],
                        "label": x["label"],
                        "area": round(float(x["area"]), 4),
                        "motion": x.get("motion"),
                    }
                    for x in humans
                ],
                target_human=hud_target if person else self._target_human,
                ultrasonic_cm=us_cm,
                mic_level=mic_level,
                mic_src=str(snap.get("mic_src") or ""),
                obstacle=obstacle,
                sensors=snap,
                message=msg,
            )

            elapsed = time.perf_counter() - loop_t
            sleep_for = period - elapsed
            if sleep_for > 0:
                end = time.time() + sleep_for
                while time.time() < end and not self._stop.is_set():
                    time.sleep(min(0.015, max(0.0, end - time.time())))

        self._send_drive(cfg.esp_base, 0, 0, force=True)
        self._set_status(running=False, left=0, right=0, mode="idle", message="Nav stopped")


FOLLOWER = PersonFollower()
