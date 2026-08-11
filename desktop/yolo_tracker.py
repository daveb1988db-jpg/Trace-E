#!/usr/bin/env python3
"""
GPU YOLO person detect for Trace-E nav.

Ultralytics YOLOv8n on CUDA. Floor-cam (~13–25 cm) sees LEGS / lower body
("long spheres"), not faces. Person class 0 with bottom-heavy / lower-frame
bias; reject face-sized top boxes. Junk filters for kids-room furniture/toys.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

Box = Tuple[int, int, int, int]


def _iou(a: Box, b: Box) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / float(max(1, union))


class YoloPersonTracker:
    def __init__(
        self,
        model_name: str = "yolov8n.pt",
        # Slightly below default so truncated child/legs survive motion blur,
        # but well above the old 0.08 junk era.
        conf: float = 0.22,
        iou: float = 0.5,
        imgsz: int = 640,
        device: Optional[str] = None,
    ) -> None:
        self.model_name = model_name
        self.conf = conf
        self.iou = iou
        self.imgsz = imgsz
        self._device = device
        self._model = None
        self._lock = threading.Lock()
        self._ready = False
        self._error = ""
        self._device_used = "cpu"
        self._infer_ms = 0.0
        self._backend = "none"
        self._next_tid = 1
        self._prev: List[Dict[str, Any]] = []

    @property
    def ready(self) -> bool:
        return self._ready

    def status(self) -> Dict[str, Any]:
        return {
            "ready": self._ready,
            "backend": self._backend,
            "device": self._device_used,
            "model": self.model_name,
            "conf": self.conf,
            "imgsz": self.imgsz,
            "infer_ms": round(self._infer_ms, 1),
            "error": self._error,
        }

    def ensure(self) -> bool:
        with self._lock:
            if self._ready and self._model is not None:
                return True
            try:
                import torch
                from ultralytics import YOLO

                if self._device:
                    dev = self._device
                elif torch.cuda.is_available():
                    dev = "0"
                else:
                    dev = "cpu"
                self._device_used = (
                    f"cuda:{dev}" if dev != "cpu" and str(dev).isdigit() else str(dev)
                )
                self._model = YOLO(self.model_name)
                dummy = np.zeros((480, 640, 3), dtype=np.uint8)
                self._model.predict(
                    dummy,
                    classes=[0],
                    conf=self.conf,
                    imgsz=self.imgsz,
                    device=dev,
                    verbose=False,
                )
                self._ready = True
                self._backend = "ultralytics-yolo"
                self._error = ""
                return True
            except Exception as exc:
                self._ready = False
                self._error = str(exc)
                self._backend = "failed"
                return False

    def _assign_ids(self, dets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Sticky IoU IDs — keep identity across single-frame misses via prev boxes.
        used_prev = set()
        out: List[Dict[str, Any]] = []
        for det in dets:
            best_i, best = -1, 0.12  # aggressive re-acquire for flicker
            for i, prev in enumerate(self._prev):
                if i in used_prev:
                    continue
                sc = _iou(det["box"], prev["box"])
                if sc > best:
                    best, best_i = sc, i
            if best_i >= 0:
                tid = int(self._prev[best_i]["track_id"])
                used_prev.add(best_i)
            else:
                tid = self._next_tid
                self._next_tid += 1
            det = dict(det)
            det["track_id"] = tid
            out.append(det)
        # Hold previous tracks briefly when this frame is empty so follow grace
        # can still IoU-match against last known boxes next frame.
        if out:
            self._prev = out
        elif self._prev:
            # Decay: keep last boxes for one miss cycle (follow owns longer grace)
            pass
        return out

    def _predict_boxes(
        self, frame_bgr: np.ndarray, device: Any, conf: Optional[float] = None
    ) -> List[Tuple[float, float, float, float, float]]:
        assert self._model is not None
        results = self._model.predict(
            frame_bgr,
            classes=[0],
            conf=float(self.conf if conf is None else conf),
            iou=self.iou,
            imgsz=self.imgsz,
            device=device,
            verbose=False,
            max_det=8,
        )
        if not results:
            return []
        boxes = getattr(results[0], "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = boxes.xyxy.cpu().numpy()
        confs = (
            boxes.conf.cpu().numpy() if boxes.conf is not None else np.ones(len(xyxy))
        )
        return [
            (float(x1), float(y1), float(x2), float(y2), float(confs[i]))
            for i, (x1, y1, x2, y2) in enumerate(xyxy)
        ]

    def _keep_personish(
        self, x: int, y: int, bw: int, bh: int, conf: float, w: int, h: int
    ) -> bool:
        """
        Floor-cam legs / lower body only (~13–25 cm cam height):
        - Accept stubby or tall vertical boxes in lower ~2/3 of frame
        - Far: smaller leg blobs still bottom-anchored
        - Reject face-sized tiny top boxes (no face / upper-body-as-face)
        Reject toys/furniture/chairs common in a kids room.
        """
        area = (bw * bh) / float(max(1, w * h))
        cy = (y + bh * 0.5) / float(max(1, h))
        top_frac = y / float(max(1, h))
        bottom_heavy = cy >= 0.48  # center in lower ~half
        in_lower_two_thirds = cy >= 0.33  # box mass in lower 2/3
        lower_frame = (y + bh) >= int(h * 0.68)  # box reaches toward floor

        # Face-sized: small, high in frame, roughly square — never nav targets
        ar = bh / float(max(1, bw))
        if top_frac < 0.28 and cy < 0.42 and area < 0.06 and 0.7 <= ar <= 1.6:
            return False
        if cy < 0.30 and area < 0.045:
            return False
        # Upper-third only (no floor anchor) — not legs from this cam
        if not in_lower_two_thirds and not lower_frame:
            return False

        # Soft floor for large lower-frame legs (not global 0.08)
        min_conf = self.conf
        if bottom_heavy and lower_frame and area >= 0.06:
            min_conf = min(self.conf, 0.16)
        elif in_lower_two_thirds and area >= 0.12:
            min_conf = min(self.conf, 0.18)
        if conf < min_conf:
            return False

        # Distant legs can be modest in pixels
        if bw < 16 or bh < 24:
            return False
        if area < 0.007:
            return False

        # Near-full-frame junk / walls — keep strict
        if area > 0.88 and conf < 0.55:
            return False
        if area > 0.72 and conf < 0.38:
            return False

        # Stubby close-ups (legs/hips "long spheres") OK if lower-frame
        if ar < 0.35:
            return False
        if ar < 0.50 and not (bottom_heavy and area >= 0.05 and conf >= 0.18):
            return False
        # Tall vertical leg columns in lower 2/3 are valid
        if ar > 5.8:
            return False
        if ar > 4.2 and not (in_lower_two_thirds and lower_frame):
            return False

        # Furniture / toy rejection (kids room):
        if cy < 0.35 and ar < 0.85 and area < 0.12 and conf < 0.45:
            return False
        if area < 0.022 and conf < 0.35:
            return False
        if 0.35 <= cy <= 0.65 and 0.04 <= area <= 0.18 and ar < 0.95 and conf < 0.40:
            return False

        return True

    def track(self, frame_bgr: np.ndarray) -> List[Dict[str, Any]]:
        if not self.ensure() or self._model is None:
            return []
        h, w = frame_bgr.shape[:2]
        # Near-black frames (covered lens / AE stuck) → never invent people from noise
        if float(np.mean(frame_bgr)) < 18.0:
            self._infer_ms = 0.0
            self._prev = []
            self._error = "cam-dark"
            return []
        t0 = time.perf_counter()
        device = 0 if self._device_used.startswith("cuda") else "cpu"
        raw: List[Tuple[float, float, float, float, float]] = []
        try:
            raw = self._predict_boxes(frame_bgr, device)
            best = max((c for *_, c in raw), default=0.0)

            # Shrink pass when nothing solid — truncated child / motion blur
            if not raw or best < 0.38:
                small = cv2.resize(
                    frame_bgr,
                    (max(64, int(w * 0.6)), max(64, int(h * 0.6))),
                    interpolation=cv2.INTER_AREA,
                )
                inv = 1.0 / 0.6
                for x1, y1, x2, y2, c in self._predict_boxes(small, device):
                    raw.append((x1 * inv, y1 * inv, x2 * inv, y2 * inv, c))
                best = max((c for *_, c in raw), default=0.0)

            # Bottom-ROI pass: floor cam → legs/lower torso live in lower frame.
            # Conf floor stays ≥0.16 (not 0.08) + same personish junk filters.
            if not raw or best < 0.36:
                y0 = int(h * 0.28)
                crop = frame_bgr[y0:h, 0:w]
                if crop.size > 0 and crop.shape[0] >= 64:
                    for x1, y1, x2, y2, c in self._predict_boxes(
                        crop, device, conf=max(0.16, self.conf - 0.04)
                    ):
                        raw.append((x1, y1 + y0, x2, y2 + y0, c))
        except Exception as exc:
            self._error = f"predict: {exc}"
            return []
        self._infer_ms = (time.perf_counter() - t0) * 1000.0

        raw.sort(key=lambda t: t[4], reverse=True)
        kept: List[Tuple[float, float, float, float, float]] = []
        for cand in raw:
            cx1, cy1, cx2, cy2, c = cand
            x = int(max(0, min(w - 1, cx1)))
            y = int(max(0, min(h - 1, cy1)))
            x2 = int(max(x + 1, min(w, cx2)))
            y2 = int(max(y + 1, min(h, cy2)))
            bw, bh = x2 - x, y2 - y
            if not self._keep_personish(x, y, bw, bh, c, w, h):
                continue
            cb = (x, y, bw, bh)
            if any(
                _iou(cb, (int(a), int(b), int(c2 - a), int(d - b))) > 0.45
                for a, b, c2, d, _ in kept
            ):
                continue
            kept.append((float(x), float(y), float(x2), float(y2), c))
            if len(kept) >= 3:
                break

        out: List[Dict[str, Any]] = []
        for x1, y1, x2, y2, conf in kept:
            bw = int(x2 - x1)
            bh = int(y2 - y1)
            x, y = int(x1), int(y1)
            out.append(
                {
                    "box": (x, y, bw, bh),
                    "conf": float(conf),
                    "cx": (x + bw * 0.5) / float(max(1, w)),
                    "cy": (y + bh * 0.5) / float(max(1, h)),
                    "area": (bw * bh) / float(max(1, w * h)),
                }
            )

        # Prefer lower-frame / bottom-heavy leg blobs over high weak boxes
        def _legs_rank(p: Dict[str, Any]) -> float:
            cy = float(p["cy"])
            area = float(p["area"])
            conf = float(p["conf"])
            lower_bias = 0.55 + 0.90 * max(0.0, min(1.0, (cy - 0.30) / 0.55))
            return conf * (0.40 + area) * lower_bias

        out.sort(key=_legs_rank, reverse=True)
        if out:
            top = out[0]
            if top["conf"] >= 0.40 or (top["area"] >= 0.08 and top["conf"] >= 0.20):
                # Keep second person only if also legs-like & strong (for cross UI)
                out = [
                    p
                    for p in out
                    if p is top
                    or (
                        p["conf"] >= 0.36
                        and p["area"] >= 0.035
                        and float(p["cy"]) >= 0.33
                    )
                ]
                out = out[:2]
            else:
                out = out[:1]

        out = self._assign_ids(out)
        out.sort(key=lambda p: p["box"][0] + p["box"][2] * 0.5)
        return out


YOLO_TRACKER = YoloPersonTracker()
