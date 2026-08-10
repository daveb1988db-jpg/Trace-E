#!/usr/bin/env python3
"""
Spiderman Trace-E Bot — Desktop Control App

PyQt5 + OpenCV control surface with zero-lag camera preview,
true differential steering (WASD), and a kid-friendly Spidey UI.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

import cv2
import numpy as np
from PyQt5.QtCore import (
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtGui import (
    QColor,
    QFont,
    QImage,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Theme — Spidey and His Amazing Friends × Trace-E Bot (bright Disney Jr HQ)
# ---------------------------------------------------------------------------

COLOR_SPIDEY_RED = "#E31C23"
COLOR_SPIDEY_BLUE = "#1E6FD9"
COLOR_SKY = "#9AD7FF"
COLOR_CREAM = "#FFF6E8"
COLOR_INK = "#141414"
COLOR_POW = "#FFE14A"
COLOR_GHOST_PINK = "#FF6BB5"
COLOR_SPIN_YELLOW = "#FFE14A"
COLOR_WHITE = "#FFFFFF"

# ESP Trace-E defaults (override with TRACE_E_ESP_BASE / ESP_BASE)
DEFAULT_ESP_HOST = os.environ.get("TRACE_E_ESP_HOST") or os.environ.get("ESP_HOST") or "192.168.1.105"
DEFAULT_ESP_BASE = (
    os.environ.get("TRACE_E_ESP_BASE")
    or os.environ.get("ESP_BASE")
    or f"http://{DEFAULT_ESP_HOST}"
).rstrip("/")

APP_QSS = f"""
QMainWindow, QWidget#CentralRoot {{
    background-color: {COLOR_SKY};
    color: {COLOR_INK};
}}

QLabel#TitleLabel {{
    color: {COLOR_POW};
    background-color: {COLOR_SPIDEY_RED};
    border: 4px solid {COLOR_INK};
    border-radius: 18px;
    font-size: 40px;
    font-weight: 900;
    letter-spacing: 2px;
    padding: 10px 18px;
}}

QLabel#SubtitleLabel {{
    color: {COLOR_SPIDEY_BLUE};
    font-size: 18px;
    font-weight: 800;
}}

QLabel#SectionHeader {{
    color: {COLOR_INK};
    font-size: 20px;
    font-weight: 900;
    padding: 4px 0;
}}

QLabel#CameraView {{
    background-color: #0B3D91;
    border: 5px solid {COLOR_SPIDEY_RED};
    border-radius: 16px;
}}

QFrame#StatusPanel, QFrame#ControllerPanel {{
    background-color: {COLOR_CREAM};
    border: 4px solid {COLOR_INK};
    border-radius: 18px;
    padding: 12px;
}}

QFrame#ControllerPanel {{
    background-color: #E5F1FF;
    border-color: {COLOR_SPIDEY_BLUE};
}}

QFrame#StatusPanel {{
    background-color: #FFE8EA;
    border-color: {COLOR_SPIDEY_RED};
}}

QLabel#StatusValue {{
    color: {COLOR_INK};
    font-size: 30px;
    font-weight: 900;
    font-family: "Comic Sans MS", "Trebuchet MS", sans-serif;
}}

QLabel#StatusHint {{
    color: #333333;
    font-size: 14px;
    font-weight: 700;
}}

QLabel#KeyHint {{
    color: {COLOR_SPIDEY_BLUE};
    font-size: 15px;
    font-weight: 800;
}}

QPushButton#GesturesBtn {{
    background-color: {COLOR_POW};
    color: {COLOR_INK};
    border: 3px solid {COLOR_INK};
    border-radius: 14px;
    font-size: 16px;
    font-weight: 900;
    padding: 8px 14px;
}}

QPushButton#GesturesBtn:checked {{
    background-color: {COLOR_SPIDEY_BLUE};
    color: {COLOR_WHITE};
}}

QLabel#GestureStatus {{
    color: {COLOR_SPIDEY_RED};
    font-size: 13px;
    font-weight: 800;
}}
"""


# ---------------------------------------------------------------------------
# Differential steering matrices (exact LM/RM in -100..+100)
# ---------------------------------------------------------------------------

DrivePair = Tuple[int, int]

# Chassis-corrected: A=spin left slow, D=spin right slow; W+A/W+D strong curves.
# (A/D were inverted on Trace/Peanut LAN bot — swapped vs classic tank docs.)
_SPIN = 36
_CIN = 8
_COUT = 100
DRIVE_MATRICES: Dict[frozenset, DrivePair] = {
    frozenset(): (0, 0),
    frozenset({"W"}): (100, 100),
    frozenset({"S"}): (-100, -100),
    frozenset({"A"}): (_SPIN, -_SPIN),
    frozenset({"D"}): (-_SPIN, _SPIN),
    frozenset({"W", "A"}): (_COUT, _CIN),
    frozenset({"W", "D"}): (_CIN, _COUT),
    frozenset({"S", "A"}): (-_COUT, -_CIN),
    frozenset({"S", "D"}): (-_CIN, -_COUT),
}


@dataclass(frozen=True)
class WheelCommand:
    left: int
    right: int

    def as_tuple(self) -> DrivePair:
        return self.left, self.right


class DifferentialSteering:
    """Maps simultaneous WASD key state to true differential LM/RM values."""

    VALID_KEYS = frozenset({"W", "A", "S", "D"})

    def __init__(self) -> None:
        self._pressed: Set[str] = set()

    @property
    def pressed_keys(self) -> Set[str]:
        return set(self._pressed)

    def set_key(self, key: str, is_down: bool) -> None:
        key = key.upper()
        if key not in self.VALID_KEYS:
            return
        if is_down:
            self._pressed.add(key)
        else:
            self._pressed.discard(key)

    def clear(self) -> None:
        self._pressed.clear()

    def compute(self) -> WheelCommand:
        """
        Resolve held keys to one of the nine drive matrices.

        Conflicting opposites (W+S or A+D) cancel on that axis so the
        result still lands on a defined matrix.
        """
        keys = set(self._pressed)

        forward = "W" in keys
        reverse = "S" in keys
        left = "A" in keys
        right = "D" in keys

        if forward and reverse:
            forward = False
            reverse = False
        if left and right:
            left = False
            right = False

        resolved: Set[str] = set()
        if forward:
            resolved.add("W")
        if reverse:
            resolved.add("S")
        if left:
            resolved.add("A")
        if right:
            resolved.add("D")

        lm, rm = DRIVE_MATRICES[frozenset(resolved)]
        return WheelCommand(left=lm, right=rm)


# ---------------------------------------------------------------------------
# ESP HTTP helpers + camera capture thread
# ---------------------------------------------------------------------------

def discover_esp_base(default_base: str = DEFAULT_ESP_BASE, timeout: float = 1.2) -> str:
    """Probe status on :80 / :8765; return working ESP base URL (no trailing slash)."""
    base = (default_base or DEFAULT_ESP_BASE).rstrip("/")
    candidates = [base]
    # Also try without port / with :8765 for status
    if "://" in base:
        host = base.split("://", 1)[1].split("/", 1)[0].split(":")[0]
        scheme = base.split("://", 1)[0]
        candidates.extend(
            [
                f"{scheme}://{host}",
                f"{scheme}://{host}:8765",
            ]
        )
    seen: Set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        for path in ("/api/status",):
            try:
                req = urllib.request.Request(
                    f"{cand}{path}",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                model = str(data.get("model") or "")
                ip = str(data.get("ip") or "").strip()
                if model and model != "trace-e":
                    continue
                if ip:
                    return f"http://{ip}"
                # Status OK on this base
                return cand.split(":8765")[0] if cand.endswith(":8765") else cand
            except Exception:
                continue
    return base


def esp_stream_url(esp_base: str) -> str:
    host = esp_base.rstrip("/")
    if "://" in host:
        host_only = host.split("://", 1)[1].split("/", 1)[0].split(":")[0]
        return f"http://{host_only}:82/stream"
    return f"http://{host}:82/stream"


def esp_drive_url(esp_base: str) -> str:
    host = esp_base.rstrip("/")
    if "://" in host:
        host_only = host.split("://", 1)[1].split("/", 1)[0].split(":")[0]
        return f"http://{host_only}:8765/api/drive"
    return f"http://{host}:8765/api/drive"


def post_drive(esp_base: str, left: int, right: int, timeout: float = 0.35) -> bool:
    url = (
        f"{esp_drive_url(esp_base)}"
        f"?{urllib.parse.urlencode({'left': int(left), 'right': int(right)})}"
    )
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= int(resp.status) < 300
    except Exception:
        return False


class CameraCaptureThread(QThread):
    """OpenCV capture — prefers ESP MJPEG :82/stream, falls back to local webcam."""

    frame_ready = pyqtSignal(object)  # numpy.ndarray (RGB)
    status_changed = pyqtSignal(str)

    def __init__(
        self,
        camera_index: int = 0,
        target_fps: int = 30,
        stream_url: Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._camera_index = camera_index
        self._target_fps = max(1, target_fps)
        self._stream_url = stream_url
        self._running = False
        self._capture: Optional[cv2.VideoCapture] = None

    def set_stream_url(self, url: Optional[str]) -> None:
        self._stream_url = url

    def stop(self) -> None:
        self._running = False

    def _open_capture(self) -> bool:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

        if self._stream_url:
            self.status_changed.emit(f"Connecting ESP cam… {self._stream_url}")
            cap = cv2.VideoCapture(self._stream_url, cv2.CAP_FFMPEG)
            if cap is not None and cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self._capture = cap
                self.status_changed.emit(f"ESP stream online · {self._stream_url}")
                return True
            if cap is not None:
                cap.release()

        self._capture = cv2.VideoCapture(self._camera_index, cv2.CAP_DSHOW)
        if self._capture is None or not self._capture.isOpened():
            if self._capture is not None:
                self._capture.release()
            self._capture = cv2.VideoCapture(self._camera_index)

        camera_ok = bool(self._capture is not None and self._capture.isOpened())
        if camera_ok:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self._capture.set(cv2.CAP_PROP_FPS, self._target_fps)
            self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.status_changed.emit(f"Local webcam {self._camera_index} online")
            return True
        self.status_changed.emit("No ESP/webcam — placeholder")
        return False

    def run(self) -> None:
        self._running = True
        frame_interval_ms = int(1000 / self._target_fps)
        camera_ok = self._open_capture()
        fail_streak = 0

        while self._running:
            if camera_ok and self._capture is not None:
                ok, frame = self._capture.read()
                if not ok or frame is None:
                    fail_streak += 1
                    if fail_streak > 15:
                        frame = self._make_placeholder("Camera lost — retrying…")
                        self.status_changed.emit("Camera lost — retrying")
                        camera_ok = self._open_capture()
                        fail_streak = 0
                    else:
                        self.msleep(40)
                        continue
                else:
                    fail_streak = 0
            else:
                frame = self._make_placeholder(
                    "Waiting for Trace-E cam\n:82/stream or local webcam"
                )
                if fail_streak % 40 == 0:
                    camera_ok = self._open_capture()
                fail_streak += 1

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            self.frame_ready.emit(np.ascontiguousarray(rgb))
            self.msleep(frame_interval_ms)

        if self._capture is not None:
            self._capture.release()
            self._capture = None

    @staticmethod
    def _make_placeholder(message: str) -> np.ndarray:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[:] = (33, 33, 33)

        cv2.rectangle(frame, (0, 0), (1280, 16), (53, 57, 229), -1)
        cv2.rectangle(frame, (0, 704), (1280, 720), (229, 136, 30), -1)

        lines = message.split("\n")
        y0 = 320 - (len(lines) - 1) * 28
        for i, line in enumerate(lines):
            text_size = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 3)[0]
            x = (1280 - text_size[0]) // 2
            y = y0 + i * 56
            cv2.putText(
                frame,
                line,
                (x, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.2,
                (255, 255, 255),
                3,
                cv2.LINE_AA,
            )

        banner = "TRACE-E BOT"
        banner_size = cv2.getTextSize(banner, cv2.FONT_HERSHEY_SIMPLEX, 1.6, 4)[0]
        bx = (1280 - banner_size[0]) // 2
        cv2.putText(
            frame,
            banner,
            (bx, 220),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.6,
            (53, 57, 229),
            4,
            cv2.LINE_AA,
        )
        return frame


# ---------------------------------------------------------------------------
# Virtual WASD overlay + Gestures (non-WASD) shortcut panel
# ---------------------------------------------------------------------------

class VirtualControllerWidget(QWidget):
    """Large on-screen W/A/S/D pad that highlights while keys are held."""

    KEY_LAYOUT = {
        "W": (1, 0),
        "A": (0, 1),
        "S": (1, 1),
        "D": (2, 1),
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._active: Set[str] = set()
        self.setMinimumSize(280, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_active_keys(self, keys: Set[str]) -> None:
        normalized = {k.upper() for k in keys}
        if normalized != self._active:
            self._active = normalized
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        cell_w = self.width() / 3.0
        cell_h = self.height() / 2.0
        pad = 8.0

        for key, (col, row) in self.KEY_LAYOUT.items():
            x = col * cell_w + pad
            y = row * cell_h + pad
            w = cell_w - pad * 2
            h = cell_h - pad * 2
            cx = int(x + w / 2)
            cy = int(y + h / 2)
            r = int(min(w, h) / 2) - 2

            active = key in self._active
            # Spidey Friends power discs: red forward/back, blue strafe
            if key in ("A", "D"):
                fill = QColor(COLOR_SPIDEY_BLUE if active else "#4DA3FF")
            else:
                fill = QColor(COLOR_SPIDEY_RED if active else "#FF3B3F")
            border = QColor(COLOR_POW if active else COLOR_INK)
            text_color = QColor(COLOR_WHITE)

            painter.setPen(QPen(border, 4))
            painter.setBrush(fill)
            painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

            font = QFont("Comic Sans MS", 26, QFont.Black)
            painter.setFont(font)
            painter.setPen(text_color)
            painter.drawText(
                int(x),
                int(y),
                int(w),
                int(h),
                Qt.AlignCenter,
                key,
            )

        painter.end()


# Non-WASD Trace-E gesture / helper shortcuts (drive stays on WASD)
GESTURE_KEYS: Tuple[Tuple[str, str], ...] = (
    ("E", "Speak"),
    ("Space", "Speak"),
    ("X", "Stop"),
    ("Esc", "Halt"),
    ("R", "Reset"),
    ("F", "Face"),
    ("↑", "Look"),
    ("↓", "Look"),
    ("←", "Look"),
    ("→", "Look"),
    ("1", "Wave"),
    ("2", "Nod"),
    ("3", "Dance"),
    ("4", "Spin"),
    ("5", "Webs"),
    ("↵", "Send"),
)


class GesturesOverlayWidget(QWidget):
    """Highlights non-WASD shortcut keys when Gestures mode is on."""

    COLS = 4

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._active: Optional[str] = None
        self._enabled = False
        self.setMinimumSize(280, 160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.hide()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.setVisible(enabled)
        if not enabled:
            self._active = None
        self.update()

    def flash_key(self, label: str) -> None:
        self._active = label
        self.update()

    def clear_flash(self) -> None:
        self._active = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        if not self._enabled:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rows = (len(GESTURE_KEYS) + self.COLS - 1) // self.COLS
        cell_w = self.width() / float(self.COLS)
        cell_h = self.height() / float(max(rows, 1))
        pad = 5.0

        for i, (key, desc) in enumerate(GESTURE_KEYS):
            col = i % self.COLS
            row = i // self.COLS
            x = col * cell_w + pad
            y = row * cell_h + pad
            w = cell_w - pad * 2
            h = cell_h - pad * 2
            active = self._active == key
            fill = QColor(COLOR_POW if active else "#4DA3FF")
            border = QColor(COLOR_INK if not active else COLOR_SPIDEY_RED)
            painter.setPen(QPen(border, 3))
            painter.setBrush(fill)
            painter.drawRoundedRect(int(x), int(y), int(w), int(h), 10, 10)

            painter.setPen(QColor(COLOR_INK if active else COLOR_WHITE))
            painter.setFont(QFont("Comic Sans MS", 14, QFont.Black))
            painter.drawText(int(x), int(y), int(w), int(h * 0.55), Qt.AlignCenter, key)
            painter.setFont(QFont("Trebuchet MS", 9, QFont.Bold))
            painter.drawText(
                int(x),
                int(y + h * 0.45),
                int(w),
                int(h * 0.5),
                Qt.AlignCenter,
                desc,
            )

        painter.end()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class TraceEControlWindow(QMainWindow):
    """Kid-friendly Spidey control deck with live camera + differential drive."""

    UI_FPS = 30

    def __init__(self, camera_index: int = 0, esp_base: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle("Spiderman Trace-E Bot — Control Deck")
        self.resize(1280, 820)
        self.setMinimumSize(960, 640)

        self._esp_base = (esp_base or DEFAULT_ESP_BASE).rstrip("/")
        self._steering = DifferentialSteering()
        self._latest_frame: Optional[np.ndarray] = None
        self._camera_status = "Starting camera…"
        self._last_command = WheelCommand(0, 0)
        self._last_sent: Optional[DrivePair] = None
        self._hw_ok = False
        self._gestures_on = False
        self._gesture_flash_timer = QTimer(self)
        self._gesture_flash_timer.setSingleShot(True)
        self._gesture_flash_timer.timeout.connect(self._clear_gesture_flash)

        self._build_ui()
        self.setStyleSheet(APP_QSS)

        stream = esp_stream_url(self._esp_base)
        self._camera_thread = CameraCaptureThread(
            camera_index=camera_index,
            target_fps=30,
            stream_url=stream,
        )
        self._camera_thread.frame_ready.connect(self._on_frame)
        self._camera_thread.status_changed.connect(self._on_camera_status)
        self._camera_thread.start()

        # Stable ~30 FPS UI render path (decoupled from capture jitter)
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(int(1000 / self.UI_FPS))
        self._render_timer.timeout.connect(self._render_frame)
        self._render_timer.start()

        # Re-dispatch while keys held so hardware failsafe stays fresh
        self._command_timer = QTimer(self)
        self._command_timer.setInterval(50)
        self._command_timer.timeout.connect(self._tick_commands)
        self._command_timer.start()

        QTimer.singleShot(200, self._probe_esp)

    # -- UI construction -----------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("CentralRoot")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        title = QLabel("TRACE-E BOT")
        title.setObjectName("TitleLabel")
        title.setAlignment(Qt.AlignCenter)

        subtitle = QLabel("Spidey and His Amazing Friends  •  Control Centre")
        subtitle.setObjectName("SubtitleLabel")
        subtitle.setAlignment(Qt.AlignCenter)

        outer.addWidget(title)
        outer.addWidget(subtitle)

        body = QHBoxLayout()
        body.setSpacing(16)
        outer.addLayout(body, stretch=1)

        # Camera column (prominent)
        cam_col = QVBoxLayout()
        cam_header = QLabel("LIVE CAMERA FEED")
        cam_header.setObjectName("SectionHeader")
        self._camera_label = QLabel()
        self._camera_label.setObjectName("CameraView")
        self._camera_label.setAlignment(Qt.AlignCenter)
        self._camera_label.setMinimumSize(640, 360)
        self._camera_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._camera_status_label = QLabel(self._camera_status)
        self._camera_status_label.setObjectName("StatusHint")
        cam_col.addWidget(cam_header)
        cam_col.addWidget(self._camera_label, stretch=1)
        cam_col.addWidget(self._camera_status_label)
        body.addLayout(cam_col, stretch=3)

        # Side panel: virtual pad + status
        side = QVBoxLayout()
        side.setSpacing(14)

        ctrl_frame = QFrame()
        ctrl_frame.setObjectName("ControllerPanel")
        ctrl_layout = QVBoxLayout(ctrl_frame)
        ctrl_header = QLabel("VIRTUAL CONTROLLER · WASD DRIVE")
        ctrl_header.setObjectName("SectionHeader")
        self._virtual_pad = VirtualControllerWidget()
        key_hint = QLabel("Hold W A S D on your keyboard")
        key_hint.setObjectName("KeyHint")
        key_hint.setAlignment(Qt.AlignCenter)

        self._gestures_btn = QPushButton("Gestures")
        self._gestures_btn.setObjectName("GesturesBtn")
        self._gestures_btn.setCheckable(True)
        self._gestures_btn.setToolTip(
            "Toggle non-WASD shortcuts: Speak, Stop, Look arrows, 1–5 poses, Space, Esc"
        )
        self._gestures_btn.toggled.connect(self._on_gestures_toggled)

        self._gestures_overlay = GesturesOverlayWidget()
        self._gesture_status = QLabel("Gestures off · WASD = drive only")
        self._gesture_status.setObjectName("GestureStatus")
        self._gesture_status.setAlignment(Qt.AlignCenter)
        self._gesture_status.setWordWrap(True)

        ctrl_layout.addWidget(ctrl_header)
        ctrl_layout.addWidget(self._virtual_pad, stretch=2)
        ctrl_layout.addWidget(key_hint)
        ctrl_layout.addWidget(self._gestures_btn)
        ctrl_layout.addWidget(self._gestures_overlay, stretch=2)
        ctrl_layout.addWidget(self._gesture_status)

        status_frame = QFrame()
        status_frame.setObjectName("StatusPanel")
        status_layout = QVBoxLayout(status_frame)
        status_header = QLabel("WHEEL SPEEDS")
        status_header.setObjectName("SectionHeader")

        speeds_grid = QGridLayout()
        lm_caption = QLabel("LEFT MOTOR (LM)")
        lm_caption.setObjectName("StatusHint")
        rm_caption = QLabel("RIGHT MOTOR (RM)")
        rm_caption.setObjectName("StatusHint")
        self._lm_value = QLabel("0")
        self._lm_value.setObjectName("StatusValue")
        self._lm_value.setAlignment(Qt.AlignCenter)
        self._rm_value = QLabel("0")
        self._rm_value.setObjectName("StatusValue")
        self._rm_value.setAlignment(Qt.AlignCenter)
        self._lm_value.setStyleSheet(f"color: {COLOR_SPIDEY_RED};")
        self._rm_value.setStyleSheet(f"color: {COLOR_SPIDEY_BLUE};")

        speeds_grid.addWidget(lm_caption, 0, 0)
        speeds_grid.addWidget(rm_caption, 0, 1)
        speeds_grid.addWidget(self._lm_value, 1, 0)
        speeds_grid.addWidget(self._rm_value, 1, 1)

        self._keys_label = QLabel("Keys: (none)")
        self._keys_label.setObjectName("StatusHint")
        self._keys_label.setAlignment(Qt.AlignCenter)

        status_layout.addWidget(status_header)
        status_layout.addLayout(speeds_grid)
        status_layout.addWidget(self._keys_label)

        side.addWidget(ctrl_frame, stretch=2)
        side.addWidget(status_frame, stretch=1)
        body.addLayout(side, stretch=1)

    # -- Camera slots / rendering --------------------------------------------

    @pyqtSlot(object)
    def _on_frame(self, frame: object) -> None:
        if isinstance(frame, np.ndarray):
            self._latest_frame = frame

    @pyqtSlot(str)
    def _on_camera_status(self, message: str) -> None:
        self._camera_status = message
        self._camera_status_label.setText(message)

    def _render_frame(self) -> None:
        frame = self._latest_frame
        if frame is None:
            return

        h, w, ch = frame.shape
        bytes_per_line = ch * w
        image = QImage(frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
        # Copy so QImage owns memory independent of numpy buffer reuse
        pixmap = QPixmap.fromImage(image.copy())
        scaled = pixmap.scaled(
            self._camera_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._camera_label.setPixmap(scaled)

    # -- Steering / hardware -------------------------------------------------

    def _probe_esp(self) -> None:
        base = discover_esp_base(self._esp_base)
        if base != self._esp_base:
            self._esp_base = base
            stream = esp_stream_url(base)
            self._camera_thread.set_stream_url(stream)
            self._camera_status_label.setText(f"ESP @ {base} · cam {stream}")
        else:
            self._camera_status_label.setText(
                f"{self._camera_status} · ESP {self._esp_base}"
            )
        # Ping drive status
        try:
            url = f"{esp_drive_url(self._esp_base).rsplit('/api/drive', 1)[0]}/api/status"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if data.get("model") == "trace-e" or data.get("ok"):
                self._hw_ok = True
                ip = data.get("ip") or self._esp_base
                self._keys_label.setText(f"ESP live · {ip}")
        except Exception:
            self._hw_ok = False

    def _tick_commands(self) -> None:
        command = self._steering.compute()
        self._last_command = command
        self._update_status_ui(command)
        self.dispatch_hardware_commands(command.left, command.right)

    def dispatch_hardware_commands(self, lm: int, rm: int) -> None:
        """POST differential L/R to ESP :8765/api/drive (−100..+100)."""
        pair = (int(lm), int(rm))
        # Always refresh non-zero so ESP failsafe (~450ms) does not coast mid-hold.
        # Skip duplicate zero spam once stopped.
        if pair == (0, 0) and self._last_sent == (0, 0):
            return
        ok = post_drive(self._esp_base, pair[0], pair[1])
        self._last_sent = pair
        self._hw_ok = ok or self._hw_ok
        print(f"LM={lm:+d}  RM={rm:+d}  esp={'ok' if ok else 'miss'}  {self._esp_base}", flush=True)

    def _update_status_ui(self, command: WheelCommand) -> None:
        self._lm_value.setText(f"{command.left:+d}")
        self._rm_value.setText(f"{command.right:+d}")
        keys = sorted(self._steering.pressed_keys)
        self._keys_label.setText("Keys: " + ("+".join(keys) if keys else "(none)"))
        self._virtual_pad.set_active_keys(self._steering.pressed_keys)

    # -- Keyboard ------------------------------------------------------------

    def _on_gestures_toggled(self, checked: bool) -> None:
        self._gestures_on = checked
        self._gestures_btn.setText("Gestures · ON" if checked else "Gestures")
        self._gestures_overlay.set_enabled(checked)
        if checked:
            self._gesture_status.setText(
                "Gestures ON · non-WASD keys lit · WASD still drive"
            )
        else:
            self._gesture_status.setText("Gestures off · WASD = drive only")
            self._clear_gesture_flash()

    def _clear_gesture_flash(self) -> None:
        self._gestures_overlay.clear_flash()

    def _fire_gesture(self, key_label: str, action: str) -> None:
        if not self._gestures_on:
            self._gestures_btn.setChecked(True)
        self._gestures_overlay.flash_key(key_label)
        self._gesture_status.setText(f"Gesture: {action}")
        print(f"GESTURE {key_label} → {action}", flush=True)
        if action in ("Stop", "Halt"):
            self._steering.clear()
            self._tick_commands()
        self._gesture_flash_timer.start(320)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            return

        gesture = self._qt_key_to_gesture(event.key())
        if gesture is not None:
            # Arrow keys: drive when gestures off; look when gestures on
            if event.key() in (
                Qt.Key_Up,
                Qt.Key_Down,
                Qt.Key_Left,
                Qt.Key_Right,
            ):
                if self._gestures_on:
                    self._fire_gesture(*gesture)
                    return
            else:
                self._fire_gesture(*gesture)
                return

        key = self._qt_key_to_wasd(event.key())
        if key:
            self._steering.set_key(key, True)
            self._tick_commands()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            return
        # Gesture keys are press-to-fire; only WASD (and arrows-as-drive) release
        if self._gestures_on and event.key() in (
            Qt.Key_Up,
            Qt.Key_Down,
            Qt.Key_Left,
            Qt.Key_Right,
        ):
            return
        key = self._qt_key_to_wasd(event.key())
        if key:
            self._steering.set_key(key, False)
            self._tick_commands()
        else:
            super().keyReleaseEvent(event)

    @staticmethod
    def _qt_key_to_wasd(qt_key: int) -> Optional[str]:
        mapping = {
            Qt.Key_W: "W",
            Qt.Key_A: "A",
            Qt.Key_S: "S",
            Qt.Key_D: "D",
            Qt.Key_Up: "W",
            Qt.Key_Left: "A",
            Qt.Key_Down: "S",
            Qt.Key_Right: "D",
        }
        return mapping.get(qt_key)

    @staticmethod
    def _qt_key_to_gesture(qt_key: int) -> Optional[Tuple[str, str]]:
        """Map non-WASD keys to (overlay label, action name)."""
        mapping = {
            Qt.Key_E: ("E", "Speak"),
            Qt.Key_Space: ("Space", "Speak"),
            Qt.Key_X: ("X", "Stop"),
            Qt.Key_Escape: ("Esc", "Halt"),
            Qt.Key_R: ("R", "Reset"),
            Qt.Key_F: ("F", "Face"),
            Qt.Key_Up: ("↑", "Look"),
            Qt.Key_Down: ("↓", "Look"),
            Qt.Key_Left: ("←", "Look"),
            Qt.Key_Right: ("→", "Look"),
            Qt.Key_1: ("1", "Wave"),
            Qt.Key_2: ("2", "Nod"),
            Qt.Key_3: ("3", "Dance"),
            Qt.Key_4: ("4", "Spin"),
            Qt.Key_5: ("5", "Webs"),
            Qt.Key_Return: ("↵", "Send"),
            Qt.Key_Enter: ("↵", "Send"),
        }
        return mapping.get(qt_key)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        # Safety stop if window loses focus while keys are held
        self._steering.clear()
        self._tick_commands()
        super().focusOutEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._render_timer.stop()
        self._command_timer.stop()
        self._steering.clear()
        self.dispatch_hardware_commands(0, 0)
        self._camera_thread.stop()
        self._camera_thread.wait(2000)
        _release_local_audio()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _release_local_audio() -> None:
    """Never leave exclusive WASAPI/pygame mixers holding the laptop speakers."""
    try:
        import pygame  # type: ignore

        if getattr(pygame, "mixer", None) is not None:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
            try:
                pygame.mixer.quit()
            except Exception:
                pass
    except Exception:
        pass


def main() -> int:
    # Prefer shared Windows audio — never force exclusive SDL/WASAPI takeover.
    os.environ.setdefault("SDL_AUDIODRIVER", "directsound")
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

    # High-DPI friendliness for modern displays
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Trace-E Bot — Spidey Friends Control Centre")
    app.setOrganizationName("Trace-E")
    app.aboutToQuit.connect(_release_local_audio)

    esp_base = discover_esp_base(DEFAULT_ESP_BASE)
    print(f"Trace-E ESP base: {esp_base}", flush=True)
    print(f"Cam stream: {esp_stream_url(esp_base)}", flush=True)
    print(f"Drive API:  {esp_drive_url(esp_base)}", flush=True)

    window = TraceEControlWindow(camera_index=0, esp_base=esp_base)
    window.show()
    try:
        return app.exec_()
    finally:
        _release_local_audio()


if __name__ == "__main__":
    sys.exit(main())
