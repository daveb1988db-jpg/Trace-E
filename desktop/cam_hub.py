#!/usr/bin/env python3
"""
Single shared camera reader for Trace-E.

ESP :82 only serves ONE client. UI + follow must both consume frames
from this hub — never open a second socket to the robot.

Latency rule: NEVER replay a backlog. Publish only the newest JPEG.
Stream mode + skip-to-latest + bounded SO_RCVBUF + periodic reconnect keeps
encode continuous while HQ polls /api/esp/latest (no browser MJPEG buffer).
"""

from __future__ import annotations

import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple

import cv2
import numpy as np

JPEG_MAX = 450_000
# Continuous :82 stream. One-shot /capture cost a fresh TCP connect + a full
# sensor round trip per frame (~8 fps, and every frame already stale on arrival).
# The stream keeps the pipe open and we only ever publish the newest JPEG in the
# buffer, so latency is one frame instead of one request. This is only safe now
# that drive commands go out over UDP — they no longer queue behind the stream's
# TCP traffic, which is why capture mode had to keep yielding the radio.
USE_STREAM = os.environ.get("TRACE_E_CAM_STREAM", "1").strip().lower() not in (
    "0",
    "false",
    "off",
)
CAPTURE_INTERVAL_FOLLOW = 0.11  # ~9 fps tiny track
CAPTURE_INTERVAL_IDLE = 0.12    # ~8 fps preview — lower lag while driving/viewing
CAPTURE_INTERVAL = CAPTURE_INTERVAL_IDLE
# The tablet app streams MJPEG straight off the ESP, so a hub pull with no
# browser watching is pure overhead: :82 takes one client, and a second video
# feed starves drive packets on the robot's single 2.4GHz radio. Park the pull
# when nobody has asked for a frame, but keep grabbing one occasionally so the
# control path still trusts this hub's IP as ground truth after a DHCP move.
IDLE_AFTER_S = 3.0
IDLE_KEEPALIVE_S = 2.5
# Was 10s to hand the radio back to teleop HTTP. With UDP control that tradeoff is
# gone, and each reconnect costs a visible STREAM_GAP_S freeze, so recycle rarely.
STREAM_RECONNECT_S = 8.0
STREAM_RCVBUF = 16 * 1024
STREAM_GAP_S = 0.12
CAPTURE_TIMEOUT_S = 1.0
CAPTURE_FAIL_BACKOFF_S = 0.45
# Brain-side downscale ceilings — a guard against a huge frame, not a target. These
# sit at VGA so the sensor's own output is forwarded byte-for-byte with no re-encode;
# anything at or below this width is never touched.
PREVIEW_MAX_W = 640
TRACK_MAX_W = 640
# Used only on the resize path. Low values here were stacking JPEG loss on top of
# the sensor's own compression, which is what made the feed look mushy.
TRACK_JPEG_Q = 85
PREVIEW_JPEG_Q = 80
# JPEG quality only. Deliberately no framesize here: the driver allocates buffers for
# the firmware's init frame_size, and asking for anything larger at runtime gets clamped
# to an arbitrary size and makes /capture hang intermittently (measured ~40% timeouts).
# Resolution is owned by CAM_FRAMESIZE in the firmware; quality is safe to set live.
CAM_QUALITY = 10


def esp_host(base: str) -> str:
    raw = (base or "").strip()
    if not raw:
        return "192.168.1.108"
    if "://" not in raw:
        raw = "http://" + raw
    return urllib.parse.urlparse(raw).hostname or "192.168.1.108"


def stream_url(esp_base: str) -> str:
    return f"http://{esp_host(esp_base)}:82/stream"


def capture_url(esp_base: str) -> str:
    return f"http://{esp_host(esp_base)}/capture"


class CamHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._esp = "http://192.168.1.108"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._jpeg: Optional[bytes] = None
        self._bgr: Optional[np.ndarray] = None
        self._seq = 0
        self._fps = 0.0
        self._error = ""
        self._running = False
        self._clients = 0
        self._last_demand = 0.0
        self._hold_until = 0.0
        self._frame_ts = 0.0
        self._mode = "idle"
        self._follow_mode = False
        self._preview_jpeg: Optional[bytes] = None
        self._interval = CAPTURE_INTERVAL_IDLE
        self._probe_n = 0
        self._oversize = False

    def hold_for(self, seconds: float) -> None:
        """Pause capture so WASD can use the Wi-Fi radio."""
        with self._lock:
            self._hold_until = max(self._hold_until, time.time() + max(0.0, seconds))

    def set_follow_mode(self, on: bool) -> None:
        """Follow wants faster grabs; idle slows so teleop keeps WiFi."""
        with self._lock:
            self._follow_mode = bool(on)
            self._interval = CAPTURE_INTERVAL_FOLLOW if self._follow_mode else CAPTURE_INTERVAL_IDLE
            esp = self._esp
            if not self._follow_mode:
                # Brief pause after Follow so the radio settles; long enough to be felt
                # as a frozen feed if overdone.
                self._hold_until = max(self._hold_until, time.time() + 0.4)
        if on:
            self._request_cam_mode(esp)

    def _request_cam_mode(self, esp_base: Optional[str] = None) -> None:
        """Ask ESP for a good JPEG quality (no-op on older FW). Never sets framesize."""
        try:
            h = esp_host(esp_base or self._esp)
            url = (
                f"http://{h}/api/camera_quality"
                f"?quality={CAM_QUALITY}&t={int(time.time())}"
            )
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1.2) as resp:
                resp.read(256)
        except Exception:
            pass

    def status(self) -> dict:
        with self._lock:
            age_ms = int(max(0.0, (time.time() - self._frame_ts) * 1000)) if self._frame_ts else None
            return {
                "running": self._running,
                "esp": self._esp,
                "seq": self._seq,
                "fps": round(self._fps, 1),
                "has_frame": self._jpeg is not None,
                "error": self._error,
                "clients": self._clients,
                "mode": self._mode,
                "age_ms": age_ms,
                "use_stream": USE_STREAM,
                "follow_mode": self._follow_mode,
                "interval_s": round(self._interval, 3),
                "demand_age_ms": (
                    int(max(0.0, (time.time() - self._last_demand) * 1000))
                    if self._last_demand
                    else None
                ),
            }

    def ensure(self, esp_base: Optional[str] = None) -> None:
        esp = (esp_base or self._esp or "http://192.168.1.108").rstrip("/")
        with self._lock:
            need = (not self._thread) or (not self._thread.is_alive()) or (esp != self._esp)
            self._esp = esp
        if need:
            self._restart(esp)

    def release(self) -> None:
        with self._lock:
            self._clients = max(0, self._clients - 1)

    def stop(self) -> None:
        self._stop.set()
        thr = None
        with self._lock:
            thr = self._thread
            self._running = False
        if thr and thr.is_alive():
            thr.join(timeout=2.0)
        with self._lock:
            self._thread = None

    def _restart(self, esp: str) -> None:
        self.stop()
        self._stop.clear()
        with self._lock:
            self._esp = esp
            self._error = ""
            self._jpeg = None  # drop stale frame — never show minutes-old JPEG
            self._bgr = None
            self._frame_ts = 0.0
            self._running = True
            self._mode = "starting"
            self._thread = threading.Thread(
                target=self._run, name="trace-cam-hub", daemon=True
            )
            self._thread.start()

    def _note_demand(self) -> None:
        with self._lock:
            self._last_demand = time.time()

    def _viewer_gone(self) -> bool:
        """True when nothing has asked for a frame recently."""
        with self._lock:
            if self._follow_mode:
                return False
            last = self._last_demand
        return (time.time() - last) > IDLE_AFTER_S

    def _idle_wait(self) -> bool:
        """Park the video pull while unwatched. Returns True if we idled."""
        if not self._viewer_gone():
            return False
        with self._lock:
            self._mode = "idle"
            self._fps = 0.0
        # ~0.4fps keepalive instead of ~19fps: enough for the control path's IP
        # self-heal to keep trusting this hub, cheap enough to leave the radio
        # to drive packets and to the tablet's own stream.
        self._grab_capture_once()
        deadline = time.time() + IDLE_KEEPALIVE_S
        while not self._stop.is_set() and time.time() < deadline:
            if not self._viewer_gone():
                break
            time.sleep(0.05)
        return True

    def latest_jpeg(self, *, preview: bool = False) -> Optional[bytes]:
        self._note_demand()
        with self._lock:
            if preview and self._preview_jpeg is not None:
                return self._preview_jpeg
            return self._jpeg

    def latest_bgr(self) -> Optional[np.ndarray]:
        self._note_demand()
        with self._lock:
            if self._bgr is not None:
                return self._bgr.copy()
            jpg = self._jpeg
        if not jpg:
            return None
        arr = np.frombuffer(jpg, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        with self._lock:
            self._bgr = bgr
            if bgr is None:
                return None
            return bgr.copy()

    def wait_jpeg(self, timeout: float = 1.0, after_seq: int = -1) -> Tuple[Optional[bytes], int]:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                if self._jpeg is not None and self._seq > after_seq:
                    return self._jpeg, self._seq
                remaining = deadline - time.time()
                if remaining <= 0:
                    return self._jpeg, self._seq
                self._cond.wait(timeout=remaining)

    @staticmethod
    def _resize_w(bgr: np.ndarray, max_w: int) -> np.ndarray:
        h, w = bgr.shape[:2]
        if w <= max_w:
            return bgr
        scale = max_w / float(w)
        return cv2.resize(
            bgr, (max_w, max(1, int(h * scale))), interpolation=cv2.INTER_AREA
        )

    @staticmethod
    def _encode(bgr: np.ndarray, quality: int) -> Optional[bytes]:
        try:
            ok, enc = cv2.imencode(
                ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
            )
            return bytes(enc) if ok else None
        except Exception:
            return None

    def _publish(self, jpg: bytes) -> None:
        if not (800 <= len(jpg) <= JPEG_MAX):
            return
        # Fast path: forward the sensor's own bytes with no decode at all. The
        # decode here existed only to measure width for the resize guard, which is
        # a waste at stream frame rates when the answer never changes — so probe
        # the width periodically instead and stay on the cheap path until a frame
        # actually comes back wider than the ceiling. latest_bgr() decodes lazily
        # for Follow/SLAM, so pixels are still there when something wants them.
        self._probe_n += 1
        if not self._oversize and (self._probe_n % 60) != 1:
            with self._cond:
                self._jpeg = jpg
                self._preview_jpeg = jpg
                self._bgr = None
                self._seq += 1
                self._frame_ts = time.time()
                self._error = ""
                self._cond.notify_all()
            return
        arr = np.frombuffer(jpg, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return
        width = bgr.shape[1]
        self._oversize = width > TRACK_MAX_W
        if width <= TRACK_MAX_W:
            track = jpg
        else:
            bgr = self._resize_w(bgr, TRACK_MAX_W)
            track = self._encode(bgr, TRACK_JPEG_Q) or jpg
        if bgr.shape[1] <= PREVIEW_MAX_W:
            preview = track
        else:
            preview = self._encode(
                self._resize_w(bgr, PREVIEW_MAX_W), PREVIEW_JPEG_Q
            ) or track
        with self._cond:
            self._jpeg = track
            self._preview_jpeg = preview
            self._bgr = bgr
            self._seq += 1
            self._frame_ts = time.time()
            self._error = ""
            self._cond.notify_all()

    def _grab_capture_once(self) -> bool:
        try:
            url = capture_url(self._esp) + f"?t={int(time.time() * 1000)}"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "TraceE-CamHub/2.2",
                    "Accept": "image/jpeg,*/*",
                    "Connection": "close",
                    "Cache-Control": "no-store",
                },
            )
            with urllib.request.urlopen(req, timeout=CAPTURE_TIMEOUT_S) as resp:
                jpg = resp.read()
            if 800 <= len(jpg) <= JPEG_MAX and jpg[:2] == b"\xff\xd8":
                self._publish(jpg)
                return True
        except Exception as exc:
            with self._lock:
                self._error = f"capture: {exc}"
            time.sleep(CAPTURE_FAIL_BACKOFF_S)
        return False

    @staticmethod
    def _latest_jpegs_in_buf(buf: bytearray) -> Optional[bytes]:
        """Return only the LAST complete JPEG in buf; discard older ones."""
        last: Optional[bytes] = None
        while True:
            soi = buf.find(b"\xff\xd8")
            if soi < 0:
                if len(buf) > 32768:
                    buf.clear()
                break
            if soi > 0:
                del buf[:soi]
            eoi = buf.find(b"\xff\xd9", 2)
            if eoi < 0:
                break
            jpg = bytes(buf[: eoi + 2])
            del buf[: eoi + 2]
            if 800 <= len(jpg) <= JPEG_MAX:
                last = jpg  # keep overwriting — only newest survives
        return last

    def _run_capture(self) -> None:
        fps_t0 = time.perf_counter()
        fps_n = 0
        with self._lock:
            self._mode = "capture"
        while not self._stop.is_set():
            with self._lock:
                hold = self._hold_until
            now = time.time()
            if hold > now:
                time.sleep(min(0.05, hold - now))
                continue
            if self._idle_wait():
                continue
            t0 = time.perf_counter()
            ok = self._grab_capture_once()
            if ok:
                fps_n += 1
                nowp = time.perf_counter()
                if nowp - fps_t0 >= 1.0:
                    with self._lock:
                        self._fps = fps_n / (nowp - fps_t0)
                    fps_t0 = nowp
                    fps_n = 0
            elapsed = time.perf_counter() - t0
            with self._lock:
                gap = float(self._interval)
            time.sleep(max(0.01, gap - elapsed))

    def _tune_sock(self, fp):
        """Tiny recv buffer + TCP_NODELAY so old JPEGs cannot sit in the kernel."""
        sock = None
        try:
            sock = fp.fp.raw._sock  # type: ignore[attr-defined]
        except Exception:
            return None
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, STREAM_RCVBUF)
        except Exception:
            pass
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        return sock

    def _drain_latest(self, sock, buf: bytearray) -> Optional[bytes]:
        """Wait for bytes, then suck the socket dry and keep only the last JPEG."""
        sock.settimeout(0.3)
        try:
            chunk = sock.recv(16384)
        except socket.timeout:
            return None
        if not chunk:
            raise ConnectionError("stream ended")
        buf.extend(chunk)
        sock.setblocking(False)
        try:
            while True:
                more = sock.recv(65536)
                if not more:
                    raise ConnectionError("stream ended")
                buf.extend(more)
                if len(buf) > JPEG_MAX * 3:
                    soi = buf.rfind(b"\xff\xd8")
                    if soi < 0:
                        buf.clear()
                    else:
                        del buf[:soi]
        except BlockingIOError:
            pass
        finally:
            try:
                sock.setblocking(True)
                sock.settimeout(0.3)
            except Exception:
                pass
        return self._latest_jpegs_in_buf(buf)

    @staticmethod
    def _close_fp(fp) -> None:
        if fp is None:
            return
        try:
            sock = fp.fp.raw._sock  # type: ignore[attr-defined]
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            fp.close()
        except Exception:
            pass

    def _note_fps(self, fps_n: int, fps_t0: float) -> Tuple[int, float]:
        nowp = time.perf_counter()
        if nowp - fps_t0 >= 1.0:
            with self._lock:
                self._fps = fps_n / (nowp - fps_t0)
            return 0, nowp
        return fps_n, fps_t0

    def _run_stream(self) -> None:
        fps_t0 = time.perf_counter()
        fps_n = 0
        failures = 0
        with self._lock:
            self._mode = "stream"
        while not self._stop.is_set():
            with self._lock:
                hold = self._hold_until
            now = time.time()
            if hold > now:
                with self._lock:
                    self._mode = "hold"
                # Do NOT /capture during teleop hold — that fights WASD for WiFi.
                # UI keeps last JPEG; stream resumes after hold expires.
                time.sleep(min(0.05, hold - now))
                continue

            if self._idle_wait():
                continue

            fp = None
            try:
                url = stream_url(self._esp)
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "TraceE-CamHub/2.2",
                        "Accept": "*/*",
                        "Connection": "close",
                        "Accept-Encoding": "identity",
                        "Cache-Control": "no-store",
                    },
                )
                fp = urllib.request.urlopen(req, timeout=1.0)
                sock = self._tune_sock(fp)
                with self._lock:
                    self._error = ""
                    self._running = True
                    self._mode = "stream"
                buf = bytearray()
                failures = 0
                t_open = time.perf_counter()
                idle_reads = 0
                while not self._stop.is_set():
                    with self._lock:
                        hold = self._hold_until
                    if hold > time.time():
                        raise ConnectionError("hold")
                    if self._viewer_gone():
                        raise ConnectionError("idle")
                    if time.perf_counter() - t_open > STREAM_RECONNECT_S:
                        raise ConnectionError("reconnect")
                    if sock is not None:
                        latest = self._drain_latest(sock, buf)
                    else:
                        chunk = fp.read(8192)
                        if not chunk:
                            raise ConnectionError("stream ended")
                        buf.extend(chunk)
                        latest = self._latest_jpegs_in_buf(buf)
                    if latest is None:
                        idle_reads += 1
                        if idle_reads > 80:
                            raise ConnectionError("no JPEG in stream")
                        continue
                    idle_reads = 0
                    self._publish(latest)
                    fps_n += 1
                    fps_n, fps_t0 = self._note_fps(fps_n, fps_t0)
            except Exception as exc:
                msg = str(exc)
                intentional = msg in ("reconnect", "hold", "idle")
                self._close_fp(fp)
                fp = None
                if intentional:
                    failures = 0
                    with self._lock:
                        self._error = ""
                        self._mode = {
                            "reconnect": "stream-reconnect",
                            "hold": "hold",
                            "idle": "idle",
                        }[msg]
                    time.sleep(STREAM_GAP_S)
                    continue
                failures += 1
                with self._lock:
                    self._error = f"stream retry {failures}: {exc}"
                    self._mode = "capture-fallback"
                # :82 is one-client. Hammering it after a timeout piles SynSent /
                # FinWait and drops us to ~0.3 fps. Grab snapshots until the
                # port can accept again.
                fallback_s = 2.5 if failures < 3 else 8.0
                fallback_until = time.time() + fallback_s
                while not self._stop.is_set() and time.time() < fallback_until:
                    with self._lock:
                        hold = self._hold_until
                    if hold > time.time():
                        time.sleep(0.05)
                        continue
                    if self._grab_capture_once():
                        fps_n += 1
                        fps_n, fps_t0 = self._note_fps(fps_n, fps_t0)
                    time.sleep(max(0.01, float(self._interval)))
                continue
            finally:
                self._close_fp(fp)

    def _run(self) -> None:
        with self._lock:
            self._running = True
        # Ask for full resolution up front, not just when Follow arms, so a fresh boot
        # is not left on whatever small framesize the sensor happened to start at.
        self._request_cam_mode()
        if USE_STREAM:
            self._run_stream()
        else:
            self._run_capture()


CAM_HUB = CamHub()
