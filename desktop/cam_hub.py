#!/usr/bin/env python3
"""
Single shared MJPEG reader for Trace-E.

ESP :82 only serves ONE client. UI + follow must both consume frames
from this hub — never open a second socket to the robot.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple

import cv2
import numpy as np

JPEG_MAX = 450_000


def esp_host(base: str) -> str:
    raw = (base or "").strip()
    if not raw:
        return "192.168.1.104"
    if "://" not in raw:
        raw = "http://" + raw
    return urllib.parse.urlparse(raw).hostname or "192.168.1.104"


def stream_url(esp_base: str) -> str:
    return f"http://{esp_host(esp_base)}:82/stream"


class CamHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._esp = "http://192.168.1.104"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._jpeg: Optional[bytes] = None
        self._bgr: Optional[np.ndarray] = None
        self._seq = 0
        self._fps = 0.0
        self._error = ""
        self._running = False
        self._clients = 0

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "esp": self._esp,
                "seq": self._seq,
                "fps": round(self._fps, 1),
                "has_frame": self._jpeg is not None,
                "error": self._error,
                "clients": self._clients,
            }

    def ensure(self, esp_base: Optional[str] = None) -> None:
        esp = (esp_base or self._esp or "http://192.168.1.104").rstrip("/")
        with self._lock:
            need = (not self._thread) or (not self._thread.is_alive()) or (esp != self._esp)
            self._esp = esp
            self._clients += 1
        if need:
            self._restart(esp)

    def release(self) -> None:
        with self._lock:
            self._clients = max(0, self._clients - 1)
            # keep stream warm while speak_server is up — only drop on stop()

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
            self._running = True
            self._thread = threading.Thread(
                target=self._run, name="trace-cam-hub", daemon=True
            )
            self._thread.start()

    def latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._jpeg

    def latest_bgr(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._bgr is None:
                return None
            return self._bgr.copy()

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

    def _run(self) -> None:
        fps_t0 = time.perf_counter()
        fps_n = 0
        stream_failures = 0
        last_capture = 0.0
        last_jpg = None
        last_change = 0.0
        while not self._stop.is_set():
            if stream_failures < 3:
                # Try MJPEG stream first
                fp = None
                try:
                    url = stream_url(self._esp)
                    req = urllib.request.Request(
                        url,
                        headers={
                            "User-Agent": "TraceE-CamHub/1.0",
                            "Accept": "*/*",
                            "Connection": "close",
                            "Accept-Encoding": "identity",
                        },
                    )
                    fp = urllib.request.urlopen(req, timeout=4)
                    with self._lock:
                        self._error = ""
                        self._running = True
                    buf = bytearray()
                    while not self._stop.is_set():
                        chunk = fp.read(8192)
                        if not chunk:
                            raise ConnectionError("stream ended")
                        buf.extend(chunk)
                        if len(buf) > JPEG_MAX * 2:
                            soi = buf.find(b"\xff\xd8")
                            if soi < 0:
                                buf.clear()
                                continue
                            del buf[:soi]
                        soi = buf.find(b"\xff\xd8")
                        if soi < 0:
                            if len(buf) > 16384:
                                buf.clear()
                            continue
                        if soi > 0:
                            del buf[:soi]
                        eoi = buf.find(b"\xff\xd9", 2)
                        if eoi < 0:
                            continue
                        jpg = bytes(buf[: eoi + 2])
                        del buf[: eoi + 2]
                        if len(jpg) < 800 or len(jpg) > JPEG_MAX:
                            continue
                        if jpg == last_jpg:
                            if time.perf_counter() - last_change > 3.0:
                                raise ConnectionError("stale stream")
                            continue
                        arr = np.frombuffer(jpg, dtype=np.uint8)
                        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        with self._cond:
                            self._jpeg = jpg
                            self._bgr = bgr
                            self._seq += 1
                            self._cond.notify_all()
                        last_jpg = jpg
                        last_change = time.perf_counter()
                        fps_n += 1
                        now = time.perf_counter()
                        if now - fps_t0 >= 1.0:
                            with self._lock:
                                self._fps = fps_n / (now - fps_t0)
                            fps_t0 = now
                            fps_n = 0
                except Exception as exc:
                    stream_failures += 1
                    with self._lock:
                        self._error = f"stream attempt {stream_failures}: {exc}"
                        self._running = False
                    time.sleep(0.4)
                finally:
                    if fp is not None:
                        try:
                            fp.close()
                        except Exception:
                            pass
            else:
                # Fallback: poll /capture (this ESP's stream may hang)
                try:
                    url = f"http://{esp_host(self._esp)}/capture"
                    req = urllib.request.Request(
                        url,
                        headers={
                            "User-Agent": "TraceE-CamHub/1.0",
                            "Accept": "image/jpeg,*/*",
                            "Connection": "close",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        jpg = resp.read()
                    if 800 <= len(jpg) <= JPEG_MAX:
                        if jpg == last_jpg:
                            if time.perf_counter() - last_change > 3.0:
                                raise ConnectionError("stale capture")
                        else:
                            arr = np.frombuffer(jpg, dtype=np.uint8)
                            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            with self._cond:
                                self._jpeg = jpg
                                self._bgr = bgr
                                self._seq += 1
                                self._cond.notify_all()
                            last_jpg = jpg
                            last_change = time.perf_counter()
                        fps_n += 1
                        now = time.perf_counter()
                        if now - fps_t0 >= 1.0:
                            with self._lock:
                                self._fps = fps_n / (now - fps_t0)
                            fps_t0 = now
                            fps_n = 0
                    now = time.perf_counter()
                    sleep = max(0.8, 1.0 - (now - last_capture))
                    last_capture = now
                    time.sleep(sleep)
                except Exception as exc:
                    with self._lock:
                        self._error = str(exc)
                        self._running = False
                    time.sleep(0.5)


CAM_HUB = CamHub()
