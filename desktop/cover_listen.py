#!/usr/bin/env python3
"""
Cover the HC-SR04 (front / 'left bumper' distance sensor) to talk to Trace.

Flow (kids toy):
  1) Hand covers ultrasonic → short hold
  2) Nav motors hold · Trace says "Listening!"
  3) Capture speech (PC mic if any, else browser/phone POST /api/listen/audio)
  4) Groq Whisper STT → kid-safe chat (Groq / Gemini) → TTS on ESP amp
  5) Resume nav

Internet: HTTPS only to Groq / Google APIs with existing keys. No third-party dumps.
"""

from __future__ import annotations

import io
import json
import os
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np
import urllib.request

COVER_CM = 12.0
COVER_HOLD_S = 0.45
COOLDOWN_S = 8.0
LISTEN_RECORD_S = 5.0
LISTEN_MAX_S = 8.0
SAMPLE_RATE = 16000

KID_SYSTEM = (
    "You are Trace-E, Ollie's friendly robot buddy (Ollie is about 6). "
    "Keep answers short (1–2 sentences), kind, playful, and age-appropriate. "
    "Call him Ollie sometimes. Never be scary, rude, or adult. Never ask for "
    "real addresses, passwords, or private family info. If asked something unsafe, "
    "gently refuse and suggest a fun safe idea. You can talk about games, animals, "
    "space, songs, and careful following. Do not claim to be a real person."
)


class CoverListenService:
    def __init__(
        self,
        *,
        esp_base: str = "http://192.168.1.104",
        speak_fn: Optional[Callable[..., dict]] = None,
        follower: Any = None,
    ) -> None:
        self.esp_base = esp_base.rstrip("/")
        self._speak = speak_fn
        self._follower = follower
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._busy = False
        self._cover_since: Optional[float] = None
        self._cooldown_until = 0.0
        self._audio_event = threading.Event()
        self._audio_bytes: Optional[bytes] = None
        self._audio_mime = "audio/wav"
        self._status: Dict[str, Any] = {
            "enabled": True,
            "phase": "idle",
            "listening": False,
            "message": "Cover the front distance sensor to talk",
            "heard": "",
            "reply": "",
            "last_error": "",
            "ultrasonic_cm": None,
            "updated": 0.0,
        }

    def attach(self, speak_fn: Callable[..., dict], follower: Any = None) -> None:
        self._speak = speak_fn
        if follower is not None:
            self._follower = follower

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _set(self, **kwargs: Any) -> None:
        with self._lock:
            self._status.update(kwargs)
            self._status["updated"] = time.time()
            self._status["listening"] = self._status.get("phase") in (
                "listening",
                "ack",
                "thinking",
                "speaking",
            )

    def start(self, esp_base: Optional[str] = None) -> None:
        if esp_base:
            self.esp_base = esp_base.rstrip("/")
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="trace-cover-listen", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def submit_audio(self, data: bytes, mime: str = "audio/wav") -> Dict[str, Any]:
        if not data or len(data) < 64:
            return {"ok": False, "error": "empty audio"}
        with self._lock:
            phase = self._status.get("phase")
            if phase not in ("listening", "ack"):
                return {"ok": False, "error": f"not listening (phase={phase})"}
            self._audio_bytes = data
            self._audio_mime = mime or "audio/wav"
            self._audio_event.set()
        return {"ok": True, "bytes": len(data)}

    def trigger_manual(self) -> Dict[str, Any]:
        """UI / API: start a listen session without covering the sensor."""
        if self._busy:
            return {"ok": False, "error": "busy"}
        threading.Thread(target=self._session, name="trace-listen-manual", daemon=True).start()
        return {"ok": True, "started": True}

    def _hold_nav(self, on: bool) -> None:
        fol = self._follower
        if fol is None:
            return
        try:
            if hasattr(fol, "set_listen_hold"):
                fol.set_listen_hold(on)
            elif on and hasattr(fol, "status") and fol.status().get("running"):
                # Soft stop motors via drive 0 — follow loop continues but hold flag preferred
                pass
        except Exception:
            pass

    def _say(self, text: str) -> None:
        if not self._speak:
            return
        try:
            self._speak(text, self.esp_base, engine=None, allow_laptop=False)
        except Exception as exc:
            self._set(last_error=f"tts: {exc}")

    def _read_us(self) -> Optional[float]:
        h = self.esp_base.replace("http://", "").replace("https://", "").split("/")[0]
        for url in (f"http://{h}:8765/api/status", f"http://{h}/api/status"):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=0.4) as resp:
                    body = json.loads(resp.read().decode("utf-8", errors="replace"))
                for key in ("ultrasonic_cm", "distance_cm", "us_cm"):
                    if body.get(key) is not None:
                        v = float(body[key])
                        if v > 0:
                            return v
            except Exception:
                continue
        # Fallback: follower sensor snapshot
        fol = self._follower
        if fol is not None:
            try:
                st = fol.status()
                us = st.get("ultrasonic_cm")
                if us is not None:
                    return float(us)
            except Exception:
                pass
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                us = self._read_us()
                self._set(ultrasonic_cm=us)
                now = time.time()
                if self._busy or now < self._cooldown_until:
                    self._cover_since = None
                elif us is not None and us <= COVER_CM:
                    if self._cover_since is None:
                        self._cover_since = now
                    elif (now - self._cover_since) >= COVER_HOLD_S:
                        self._cover_since = None
                        self._cooldown_until = now + COOLDOWN_S
                        threading.Thread(
                            target=self._session, name="trace-listen-session", daemon=True
                        ).start()
                else:
                    self._cover_since = None
            except Exception as exc:
                self._set(last_error=str(exc))
            self._stop.wait(0.12)

    def _session(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._audio_event.clear()
        self._audio_bytes = None
        try:
            self._hold_nav(True)
            self._set(phase="listening", message="I'm listening — talk now", heard="", reply="", last_error="")
            # Don't block the whole session if amp/ESP is slow
            threading.Thread(target=lambda: self._say("Listening!"), daemon=True).start()

            wav = self._capture_speech()
            if wav is None:
                self._set(phase="idle", message="I didn't catch that — cover sensor and try again")
                threading.Thread(
                    target=lambda: self._say(
                        "Sorry, I did not hear you. Cover the sensor and try again."
                    ),
                    daemon=True,
                ).start()
                return

            self._set(phase="thinking", message="Thinking…")
            heard = self._transcribe(wav)
            if not heard:
                self._set(phase="idle", message="Couldn't understand — try again", heard="")
                threading.Thread(
                    target=lambda: self._say("Hmm, say that again a bit louder."),
                    daemon=True,
                ).start()
                return

            self._set(heard=heard, message=f"Heard: {heard[:80]}")
            reply = self._kid_chat(heard)
            self._set(reply=reply, phase="speaking", message=reply[:120])
            self._say(reply)  # wait for reply to finish playing
            self._set(phase="idle", message="Cover the sensor to talk again")
        except Exception as exc:
            self._set(phase="idle", last_error=str(exc), message="Listen failed")
            try:
                self._say("Oops, my ears glitched. Try again.")
            except Exception:
                pass
        finally:
            self._hold_nav(False)
            self._busy = False
            self._cooldown_until = time.time() + COOLDOWN_S

    def _capture_speech(self) -> Optional[Path]:
        # 1) Trace's own MAX4466 via firmware /api/mic_wav (preferred)
        esp_wav = self._capture_esp_mic()
        if esp_wav is not None:
            return esp_wav

        # 2) Browser/phone upload while listening
        if self._audio_event.wait(timeout=1.0):
            data = self._audio_bytes
            self._audio_bytes = None
            if data:
                return self._bytes_to_wav(data, self._audio_mime)

        # Wait a bit more for browser if ESP mic endpoint missing
        if self._audio_event.wait(timeout=LISTEN_MAX_S):
            data = self._audio_bytes
            self._audio_bytes = None
            if data:
                return self._bytes_to_wav(data, self._audio_mime)

        # 3) Last resort: PC mic if present
        try:
            import sounddevice as sd
        except Exception:
            return None
        try:
            devices = sd.query_devices()
            ins = [i for i, d in enumerate(devices) if int(d.get("max_input_channels") or 0) > 0]
            if not ins:
                return None
            device = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else None
            rec = sd.rec(
                int(LISTEN_RECORD_S * SAMPLE_RATE),
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=device if device is not None and device >= 0 else ins[0],
                blocking=True,
            )
            pcm = np.clip(rec.reshape(-1), -1.0, 1.0)
            if float(np.max(np.abs(pcm))) < 0.02:
                return None
            path = Path(tempfile.gettempdir()) / f"trace_listen_{int(time.time())}.wav"
            self._write_wav(path, pcm, SAMPLE_RATE)
            return path
        except Exception as exc:
            self._set(last_error=f"mic: {exc}")
            return None

    def _capture_esp_mic(self) -> Optional[Path]:
        h = self.esp_base.replace("http://", "").replace("https://", "").split("/")[0]
        for base in (f"http://{h}:8765", f"http://{h}"):
            url = f"{base}/api/mic_wav?seconds=3.2"
            try:
                self._set(message="Recording on Trace mic…")
                req = urllib.request.Request(url, headers={"Accept": "audio/wav,*/*"})
                with urllib.request.urlopen(req, timeout=18) as resp:
                    data = resp.read()
                if not data or len(data) < 1000 or data[:4] != b"RIFF":
                    continue
                path = Path(tempfile.gettempdir()) / f"trace_esp_mic_{int(time.time())}.wav"
                path.write_bytes(data)
                return path
            except Exception as exc:
                self._set(last_error=f"esp mic: {exc}")
                continue
        return None

    def _bytes_to_wav(self, data: bytes, mime: str) -> Optional[Path]:
        path = Path(tempfile.gettempdir()) / f"trace_listen_up_{int(time.time())}"
        mime = (mime or "").lower()
        try:
            if "wav" in mime or data[:4] == b"RIFF":
                out = path.with_suffix(".wav")
                out.write_bytes(data)
                return out
            # webm/ogg — leave as-is for Groq (accepts webm); also try wav wrapper fail soft
            ext = ".webm" if "webm" in mime else ".ogg" if "ogg" in mime else ".bin"
            out = path.with_suffix(ext)
            out.write_bytes(data)
            return out
        except Exception:
            return None

    @staticmethod
    def _write_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
        arr = (pcm * 32767.0).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(arr.tobytes())

    def _transcribe(self, path: Path) -> str:
        key = (os.environ.get("GROQ_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY missing for speech-to-text")
        from groq import Groq

        client = Groq(api_key=key)
        with path.open("rb") as f:
            tr = client.audio.transcriptions.create(
                file=(path.name, f.read()),
                model="whisper-large-v3",
                language="en",
                response_format="text",
                temperature=0.0,
            )
        if isinstance(tr, str):
            return tr.strip()
        text = getattr(tr, "text", None) or str(tr)
        return str(text).strip()

    def _kid_chat(self, heard: str) -> str:
        heard = (heard or "").strip()[:400]
        if not heard:
            return "I am listening. Tell me something fun!"

        # Local nav shortcuts still work without cloud chat
        low = heard.lower()
        if any(w in low for w in ("stop", "halt", "nav off", "stop follow")):
            fol = self._follower
            if fol is not None and hasattr(fol, "stop"):
                try:
                    fol.stop("voice halt")
                except Exception:
                    pass
            return "Okay — I will stay still."
        if any(w in low for w in ("follow me", "nav on", "come here", "follow ollie")):
            fol = self._follower
            if fol is not None and hasattr(fol, "start"):
                try:
                    fol.start(esp_base=self.esp_base, target_human=1)
                except Exception:
                    pass
            return "Okay Ollie — I will try to follow carefully."

        # Music / YouTube
        try:
            from youtube_play import YT_PLAYER, parse_music_command

            q = parse_music_command(heard)
            if q and YT_PLAYER is not None:
                YT_PLAYER.play_async(q)
                return f"Okay Ollie — playing {q}!"
        except Exception:
            pass

        groq_key = (os.environ.get("GROQ_API_KEY") or "").strip()
        if groq_key:
            try:
                from groq import Groq

                client = Groq(api_key=groq_key)
                resp = client.chat.completions.create(
                    model=os.environ.get("TRACE_E_CHAT_MODEL")
                    or "llama-3.1-8b-instant",
                    messages=[
                        {"role": "system", "content": KID_SYSTEM},
                        {"role": "user", "content": heard},
                    ],
                    temperature=0.6,
                    max_tokens=80,
                )
                out = (resp.choices[0].message.content or "").strip()
                if out:
                    return out[:280]
            except Exception as exc:
                self._set(last_error=f"groq chat: {exc}")

        gem_key = (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or ""
        ).strip()
        if gem_key:
            try:
                from google import genai

                client = genai.Client(api_key=gem_key)
                resp = client.models.generate_content(
                    model=os.environ.get("TRACE_E_GEMINI_CHAT") or "gemini-2.0-flash",
                    contents=f"{KID_SYSTEM}\n\nChild said: {heard}",
                )
                out = (getattr(resp, "text", None) or "").strip()
                if out:
                    return out[:280]
            except Exception as exc:
                self._set(last_error=f"gemini chat: {exc}")

        return "That sounds cool! Want to play follow-the-leader?"


COVER_LISTEN = CoverListenService()
