#!/usr/bin/env python3
"""
Trace-E Web-Quarters local brain — serve mock_ui + Talk APIs.

Endpoints:
  GET  /                  -> mock_ui.html
  GET  /api/health
  POST /api/talk          -> Talk TO Trace (chat/command stub)
  POST /api/speak         -> Talk THROUGH Trace (TTS -> ESP amp, laptop fallback)
  POST /api/follow/start  -> Person follow (cam detect -> differential drive)
  POST /api/follow/stop
  GET  /api/follow/status
  GET  /api/follow/frame  -> Annotated JPEG overlay

TTS (Peanut Ana PRIMARY for Ollie):
  1) edge-tts en-US-AnaNeural  (Peanut's working Ana voice)
  2) Groq Orpheus TTS          (GROQ_API_KEY)
  3) Gemini TTS                (GEMINI_API_KEY / GOOGLE_API_KEY)
  4) optional 101Soundboards Spidey
  5) pyttsx3 only as last resort

Playback: AMP-FIRST (MAX98357A on ESP :8765). Default amp-only —
laptop only if amp hard-fails AND allow_laptop=1 / TRACE_E_ALLOW_LAPTOP=1.
NO chirps on WASD (drive stays on ESP from the page).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DESKTOP_DIR = Path(__file__).resolve().parent
REPO_DIR = DESKTOP_DIR.parent
CACHE_DIR = DESKTOP_DIR / "_tts_cache"
SFX_DIR = DESKTOP_DIR / "assets" / "sfx"
CACHE_DIR.mkdir(exist_ok=True)
SFX_DIR.mkdir(parents=True, exist_ok=True)

# Person follow (OpenCV HOG -> differential drive). Optional ROS2 Docker wraps same module.
try:
    from cam_hub import CAM_HUB
except Exception:  # pragma: no cover
    CAM_HUB = None  # type: ignore
try:
    from follow_person import FOLLOWER as PERSON_FOLLOWER
except Exception:  # pragma: no cover
    PERSON_FOLLOWER = None  # type: ignore
try:
    from cover_listen import COVER_LISTEN
except Exception:  # pragma: no cover
    COVER_LISTEN = None  # type: ignore
try:
    from youtube_play import YT_PLAYER, parse_music_command
except Exception:  # pragma: no cover
    YT_PLAYER = None  # type: ignore
    parse_music_command = None  # type: ignore

# Load API keys from Trace-E .env and/or peanut-robot .env (never commit real .env)
def _load_dotenv_files() -> List[str]:
    loaded: List[str] = []
    try:
        from dotenv import load_dotenv
    except Exception:
        return loaded
    peanut_root = Path(os.environ.get("PEANUT_ROOT") or r"C:\Users\Bartl\Documents\peanut-robot")
    # Peanut first (API keys), then Trace overrides (ESP IP, amp-only, chirps)
    base_first = [peanut_root / ".env"]
    override_last = [REPO_DIR / ".env", DESKTOP_DIR / ".env"]
    for path in base_first:
        try:
            if path.is_file():
                load_dotenv(path, override=False)
                loaded.append(str(path))
        except Exception:
            continue
    for path in override_last:
        try:
            if path.is_file():
                load_dotenv(path, override=True)
                loaded.append(str(path) + " (override)")
        except Exception:
            continue
    return loaded


_DOTENV_LOADED = _load_dotenv_files()

# Situation -> candidate WAV filenames (replace files in assets/sfx/ with real Spidey clips)
SFX_MAP = {
    "connect": ["connect.wav", "chirp1.wav"],
    "success": ["success.wav", "chirp2.wav", "chirp3.wav"],
    "talk_heard": ["chirp1.wav", "chirp2.wav", "success.wav"],
    "speak_ok": ["success.wav", "chirp3.wav"],
    "alert": ["alert.wav"],
    "error": ["alert.wav", "stop.wav"],
    "stop": ["stop.wav"],
    "move_start": ["move_start.wav", "chirp1.wav"],
    "turn": ["turn.wav", "chirp2.wav"],
    "gesture": ["gesture.wav", "chirp3.wav"],
    "random": ["chirp1.wav", "chirp2.wav", "chirp3.wav", "gesture.wav"],
}

HOST = os.environ.get("TRACE_E_SPEAK_HOST", "0.0.0.0")
PORT = int(os.environ.get("TRACE_E_SPEAK_PORT", "8787"))
# Chirps default OFF — no laptop/ESP tones unless client sends mode=situational|random
CHIRPS_DEFAULT = (os.environ.get("TRACE_E_CHIRPS") or "off").strip().lower()
# Amp-only by default — laptop speakers are opt-in fallback only
ALLOW_LAPTOP = (os.environ.get("TRACE_E_ALLOW_LAPTOP") or "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
DEFAULT_ESP = (
    os.environ.get("TRACE_E_ESP_BASE")
    or "http://192.168.1.104"
).rstrip("/")
# Peanut's ESP_BASE_URL may point at a different robot — only used as discover hint
_PEANUT_ESP_HINT = (
    os.environ.get("ESP_BASE") or os.environ.get("ESP_BASE_URL") or ""
).rstrip("/")

# Peanut Ana + dual-key cloud TTS
ANA_VOICE = os.environ.get("TRACE_E_ANA_VOICE") or os.environ.get("ANA_VOICE") or "en-US-AnaNeural"
ANA_RATE = os.environ.get("TRACE_E_ANA_RATE") or os.environ.get("ANA_RATE") or "-5%"
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip()
GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or os.environ.get("GOOGLE_GENAI_API_KEY")
    or ""
).strip()
GROQ_TTS_MODEL = (
    os.environ.get("GROQ_TTS_MODEL") or "canopylabs/orpheus-v1-english"
).strip()
# Hannah ≈ young/friendly English Orpheus voice (Peanut-ish for Ollie)
GROQ_TTS_VOICE = (os.environ.get("GROQ_TTS_VOICE") or "hannah").strip()
GEMINI_TTS_MODEL = (
    os.environ.get("GEMINI_TTS_MODEL")
    or "gemini-2.5-flash-preview-tts"
).strip()
# Kore ≈ bright kid-friendly Gemini prebuilt voice
GEMINI_TTS_VOICE = (os.environ.get("GEMINI_TTS_VOICE") or "Kore").strip()
# peanut-auto | peanut | ana | groq | gemini | spidey | pyttsx3
DEFAULT_TTS_ENGINE = (
    os.environ.get("TRACE_E_TTS_ENGINE") or "peanut-auto"
).strip().lower()

# Spidey TTS board (optional secondary)
SPIDEY_TTS_URL = os.environ.get(
    "TRACE_E_101_TTS_URL",
    "https://www.101soundboards.com/tts/1038002-spider-man-christopher-daniel-barnes-marvel-sq-tts-text-to-speech",
)
SPIDEY_BOARD_ID = os.environ.get("TRACE_E_101_BOARD_ID", "1038002")
SPIDEY_TTS_ENABLED = (os.environ.get("TRACE_E_SPIDEY_TTS") or "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_talk_lock = threading.Lock()
_last_talk: Dict[str, Any] = {"text": "", "reply": "", "ts": 0}


def _json_bytes(obj: dict, code: int = 200) -> Tuple[int, bytes, str]:
    return code, json.dumps(obj).encode("utf-8"), "application/json"


def discover_esp(preferred: str = DEFAULT_ESP, timeout: float = 0.35, quick: bool = False) -> str:
    preferred = (preferred or DEFAULT_ESP).rstrip("/")
    hosts = []
    for base in (preferred, DEFAULT_ESP, "http://192.168.1.104", _PEANUT_ESP_HINT, "http://192.168.1.105"):
        if not base:
            continue
        try:
            h = urllib.parse.urlparse(base if "://" in base else f"http://{base}").hostname
            if h and h not in hosts:
                hosts.append(h)
        except Exception:
            pass
    if not quick:
        for n in (104, 105, 106, 102, 100):
            h = f"192.168.1.{n}"
            if h not in hosts:
                hosts.append(h)

    for h in hosts[: (3 if quick else 8)]:
        for port in (8765, 80):
            url = f"http://{h}:{port}/api/status" if port != 80 else f"http://{h}/api/status"
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                model = str(data.get("model") or "")
                if model and model not in ("trace-e", "peanut", ""):
                    continue
                ip = str(data.get("ip") or h).strip() or h
                return f"http://{ip}"
            except Exception:
                continue
    return preferred if preferred.startswith("http") else f"http://{preferred}"


def _http_get(url: str, timeout: float = 25.0) -> Tuple[int, bytes, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "*/*",
            "Referer": "https://www.101soundboards.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read(), resp.headers.get("Content-Type", "")


def _http_post_form(url: str, data: dict, timeout: float = 45.0) -> Tuple[int, bytes, str]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/json,*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": SPIDEY_TTS_URL,
            "Origin": "https://www.101soundboards.com",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read(), resp.headers.get("Content-Type", "")


def _key_status() -> Dict[str, Any]:
    return {
        "groq": bool(GROQ_API_KEY),
        "gemini": bool(GEMINI_API_KEY),
        "dotenv_loaded": list(_DOTENV_LOADED),
        "ana_voice": ANA_VOICE,
        "groq_tts_model": GROQ_TTS_MODEL,
        "groq_tts_voice": GROQ_TTS_VOICE,
        "gemini_tts_model": GEMINI_TTS_MODEL,
        "gemini_tts_voice": GEMINI_TTS_VOICE,
        "default_engine": DEFAULT_TTS_ENGINE,
        "spidey_optional": SPIDEY_TTS_ENABLED,
    }


def _ffmpeg_exe() -> Optional[str]:
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _write_pcm_wav(path: Path, pcm: bytes, rate: int = 16000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(pcm)
    return path


def _resample_pcm16_mono(pcm: bytes, src_rate: int, dst_rate: int = 16000) -> bytes:
    if not pcm or src_rate <= 0 or src_rate == dst_rate:
        return pcm
    samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    ratio = float(dst_rate) / float(src_rate)
    out_n = max(1, int(len(samples) * ratio))
    out = []
    for i in range(out_n):
        src_i = min(len(samples) - 1, int(i / ratio))
        out.append(samples[src_i])
    return struct.pack("<" + "h" * len(out), *out)


def ensure_wav_16k_mono(src: Path) -> Optional[Path]:
    """Normalize any audio file to 16 kHz mono PCM WAV for ESP amp."""
    if not src or not src.exists():
        return None
    out = CACHE_DIR / f"{src.stem}_16k.wav"
    if out.exists() and out.stat().st_size > 44 and src.suffix.lower() == ".wav":
        # Still re-check rate below if cheap
        pass
    ffmpeg = _ffmpeg_exe()
    if ffmpeg:
        try:
            import subprocess

            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(src),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(out),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if out.exists() and out.stat().st_size > 44:
                return out
        except Exception:
            pass
    if src.suffix.lower() == ".mp3":
        return mp3_to_wav_16k(src)
    if src.suffix.lower() == ".wav":
        try:
            with wave.open(str(src), "rb") as w:
                nch, sw, fr, nframes, _, _ = w.getparams()[:6]
                frames = w.readframes(nframes)
            if sw == 1:
                frames = b"".join(struct.pack("<h", (b - 128) * 256) for b in frames)
                sw = 2
            if nch > 1 and sw == 2:
                samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
                mono = [
                    int(sum(samples[i : i + nch]) / nch) for i in range(0, len(samples), nch)
                ]
                frames = struct.pack("<" + "h" * len(mono), *mono)
                nch = 1
            if fr != 16000 and sw == 2 and nch == 1:
                frames = _resample_pcm16_mono(frames, fr, 16000)
                fr = 16000
            return _write_pcm_wav(out, frames, rate=fr or 16000, channels=1)
        except Exception:
            return src if src.exists() else None
    return None


def synth_edge_ana_wav(text: str) -> Path:
    """Peanut Ana voice via edge-tts (en-US-AnaNeural) -> 16 kHz mono WAV."""
    import asyncio

    import edge_tts

    text = (text or "").strip()
    key = hashlib.sha1(f"ana|{ANA_VOICE}|{ANA_RATE}|{text}".encode("utf-8")).hexdigest()[:16]
    wav_path = CACHE_DIR / f"ana_{key}.wav"
    if wav_path.exists() and wav_path.stat().st_size > 44:
        return wav_path
    mp3_path = CACHE_DIR / f"ana_{key}.mp3"

    async def _go() -> None:
        communicate = edge_tts.Communicate(text, ANA_VOICE, rate=ANA_RATE)
        await communicate.save(str(mp3_path))

    try:
        asyncio.run(_go())
    except RuntimeError:
        # Nested event loop (rare) — use a fresh loop in a thread
        import concurrent.futures

        def _runner() -> None:
            asyncio.run(_go())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_runner).result(timeout=40)

    norm = ensure_wav_16k_mono(mp3_path)
    if not norm:
        raise RuntimeError("edge-tts Ana produced no wav (ffmpeg/mp3 convert failed)")
    if norm != wav_path:
        try:
            wav_path.write_bytes(norm.read_bytes())
        except Exception:
            return norm
    return wav_path


def synth_groq_tts_wav(text: str) -> Path:
    """Groq Orpheus/PlayAI TTS -> 16 kHz mono WAV (needs GROQ_API_KEY)."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY missing")
    from groq import Groq

    text = (text or "").strip()
    # Light vocal direction — bubbly Peanut-for-Ollie, not a tour guide
    spoken = f"[cheerful, friendly kid companion] {text}"
    key = hashlib.sha1(
        f"groq|{GROQ_TTS_MODEL}|{GROQ_TTS_VOICE}|{spoken}".encode("utf-8")
    ).hexdigest()[:16]
    wav_path = CACHE_DIR / f"groq_{key}.wav"
    if wav_path.exists() and wav_path.stat().st_size > 44:
        return wav_path

    client = Groq(api_key=GROQ_API_KEY)
    raw_path = CACHE_DIR / f"groq_{key}_raw.wav"
    attempts = [
        (GROQ_TTS_MODEL, GROQ_TTS_VOICE, spoken),
        # Orpheus may require console terms acceptance — PlayAI still works on many keys
        ("playai-tts", "Cheyenne-PlayAI", text),
        ("playai-tts", "Fritz-PlayAI", text),
    ]
    last_err: Optional[Exception] = None
    for model, voice, inp in attempts:
        try:
            response = client.audio.speech.create(
                model=model,
                voice=voice,
                input=inp,
                response_format="wav",
            )
            if hasattr(response, "write_to_file"):
                response.write_to_file(str(raw_path))
            else:
                data = getattr(response, "read", None)
                if callable(data):
                    raw_path.write_bytes(data())
                else:
                    raw_path.write_bytes(bytes(response))
            if raw_path.exists() and raw_path.stat().st_size > 44:
                break
        except Exception as exc:
            last_err = exc
            continue
    else:
        raise RuntimeError(f"Groq TTS failed: {last_err}")

    norm = ensure_wav_16k_mono(raw_path)
    if not norm:
        raise RuntimeError("Groq TTS convert failed")
    if norm != wav_path:
        wav_path.write_bytes(norm.read_bytes())
    return wav_path


def synth_gemini_tts_wav(text: str) -> Path:
    """Gemini native TTS -> 16 kHz mono WAV (needs GEMINI_API_KEY / GOOGLE_API_KEY)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY missing")
    from google import genai
    from google.genai import types

    text = (text or "").strip()
    spoken = f"Say cheerfully, like a bubbly kid-friendly robot friend named Peanut: {text}"
    key = hashlib.sha1(
        f"gem|{GEMINI_TTS_MODEL}|{GEMINI_TTS_VOICE}|{spoken}".encode("utf-8")
    ).hexdigest()[:16]
    wav_path = CACHE_DIR / f"gemini_{key}.wav"
    if wav_path.exists() and wav_path.stat().st_size > 44:
        return wav_path

    client = genai.Client(api_key=GEMINI_API_KEY)
    models_try = [
        GEMINI_TTS_MODEL,
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-preview-tts",
        "gemini-3.1-flash-tts-preview",
    ]
    last_err: Optional[Exception] = None
    pcm: Optional[bytes] = None
    rate = 24000
    for model in models_try:
        try:
            response = client.models.generate_content(
                model=model,
                contents=spoken,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=GEMINI_TTS_VOICE,
                            )
                        )
                    ),
                ),
            )
            part = response.candidates[0].content.parts[0].inline_data
            data = part.data
            if isinstance(data, str):
                import base64

                pcm = base64.b64decode(data)
            else:
                pcm = bytes(data)
            mime = (getattr(part, "mime_type", None) or "") + ""
            m = re.search(r"rate=(\d+)", mime, re.I)
            if m:
                rate = int(m.group(1))
            if pcm and len(pcm) > 100:
                break
        except Exception as exc:
            last_err = exc
            pcm = None
            continue
    if not pcm:
        raise RuntimeError(f"Gemini TTS failed: {last_err}")
    pcm16 = _resample_pcm16_mono(pcm, rate, 16000)
    return _write_pcm_wav(wav_path, pcm16, rate=16000, channels=1)


def try_101_spidey_tts(text: str) -> Optional[Path]:
    """Best-effort 101Soundboards Spidey TTS -> cached mp3. Returns None on CF/block/fail."""
    if not SPIDEY_TTS_ENABLED:
        return None
    text = (text or "").strip()
    if not text:
        return None
    key = hashlib.sha1(f"101|{SPIDEY_BOARD_ID}|{text}".encode("utf-8")).hexdigest()[:16]
    out = CACHE_DIR / f"spidey_{key}.mp3"
    if out.exists() and out.stat().st_size > 500:
        return out

    candidates = [
        ("https://www.101soundboards.com/sounds", {
            "sound[sound_transcript]": text,
            "sound[board_id]": SPIDEY_BOARD_ID,
            "board_id": SPIDEY_BOARD_ID,
            "text": text,
            "tts": "1",
        }),
        (f"https://www.101soundboards.com/tts/{SPIDEY_BOARD_ID}/generate", {
            "text": text,
            "phrase": text,
            "board_id": SPIDEY_BOARD_ID,
        }),
        ("https://www.101soundboards.com/tts/generate", {
            "text": text,
            "board_id": SPIDEY_BOARD_ID,
            "tts_text": text,
        }),
    ]

    html = ""
    try:
        # Warm session / page (often CF challenge — still try POST after)
        _http_get(SPIDEY_TTS_URL, timeout=12)
    except Exception:
        pass

    for url, form in candidates:
        try:
            code, raw, ct = _http_post_form(url, form)
            html = raw.decode("utf-8", errors="replace")
            if code >= 400:
                continue
            # Direct audio?
            if "audio" in (ct or "") or raw[:3] == b"ID3" or raw[:2] == b"\xff\xfb":
                out.write_bytes(raw)
                return out
            m = re.search(
                r'(https?://[^\"\'\s>]+\.mp3[^\"\'\s>]*)|(/[^\"\'\s>]+\.mp3[^\"\'\s>]*)',
                html,
                re.I,
            )
            if not m:
                m = re.search(r'data-sound-url=[\"\']([^\"\']+)[\"\']', html, re.I)
            if not m:
                continue
            mp3_url = m.group(1) or m.group(0)
            if mp3_url.startswith("/"):
                mp3_url = "https://www.101soundboards.com" + mp3_url
            code2, audio, _ = _http_get(mp3_url, timeout=40)
            if code2 == 200 and len(audio) > 500:
                out.write_bytes(audio)
                return out
        except Exception:
            continue
    return None


def synth_pyttsx3_wav(text: str) -> Path:
    """Peanut-style local robot voice -> 16-bit mono WAV @ 16 kHz."""
    import pyttsx3

    key = hashlib.sha1(f"pyttsx3|{text}".encode("utf-8")).hexdigest()[:16]
    wav_path = CACHE_DIR / f"robot_{key}.wav"
    if wav_path.exists() and wav_path.stat().st_size > 44:
        return wav_path

    # pyttsx3 saves via SAPI; use temp then resample/normalize to PCM wav
    tmp_wav = CACHE_DIR / f"_tmp_{key}.wav"
    engine = pyttsx3.init()
    try:
        rate = engine.getProperty("rate")
        engine.setProperty("rate", int(rate * 0.92) if isinstance(rate, int) else 175)
        # Prefer a higher / robotic-ish voice if present
        try:
            voices = engine.getProperty("voices") or []
            for v in voices:
                name = (getattr(v, "name", "") or "").lower()
                if any(x in name for x in ("david", "mark", "zira", "hazel", "male")):
                    engine.setProperty("voice", v.id)
                    break
        except Exception:
            pass
        engine.save_to_file(text, str(tmp_wav))
        engine.runAndWait()
    finally:
        try:
            engine.stop()
        except Exception:
            pass

    # Re-encode to clean 16k mono PCM for ESP
    with wave.open(str(tmp_wav), "rb") as src:
        nch, sw, fr, nframes, _, _ = src.getparams()[:6]
        frames = src.readframes(nframes)
    if sw != 2:
        # Expand 8-bit if needed
        if sw == 1:
            frames = b"".join(
                struct.pack("<h", (b - 128) * 256) for b in frames
            )
            sw = 2
    # Downmix
    if nch > 1:
        samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
        mono = []
        for i in range(0, len(samples), nch):
            chunk = samples[i : i + nch]
            mono.append(int(sum(chunk) / len(chunk)))
        frames = struct.pack("<" + "h" * len(mono), *mono)
        nch = 1
    # Naive resample to 16000
    if fr != 16000 and fr > 0:
        samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
        ratio = 16000.0 / float(fr)
        out_n = int(len(samples) * ratio)
        out_s = []
        for i in range(out_n):
            src_i = int(i / ratio)
            if src_i >= len(samples):
                src_i = len(samples) - 1
            out_s.append(samples[src_i])
        frames = struct.pack("<" + "h" * len(out_s), *out_s)
        fr = 16000

    with wave.open(str(wav_path), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(fr or 16000)
        dst.writeframes(frames)
    try:
        tmp_wav.unlink(missing_ok=True)
    except Exception:
        pass
    return wav_path


def mp3_to_wav_16k(mp3_path: Path) -> Optional[Path]:
    """Convert mp3->wav via pydub/ffmpeg if available; else None."""
    wav_path = mp3_path.with_suffix(".wav")
    if wav_path.exists() and wav_path.stat().st_size > 44:
        return wav_path
    try:
        from pydub import AudioSegment  # type: ignore

        seg = AudioSegment.from_file(str(mp3_path))
        seg = seg.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        seg.export(str(wav_path), format="wav")
        return wav_path
    except Exception:
        return None


def play_laptop(wav_path: Path) -> bool:
    """Play WAV on the laptop using shared-mode backends (avoid exclusive WASAPI).

    Order: winsound (shared WinMM) -> pygame DirectSound shared -> pygame default.
    """
    path = str(wav_path)
    # 1) winsound / WinMM — always shared, survives exclusive-mode apps better
    try:
        import winsound

        winsound.PlaySound(path, winsound.SND_FILENAME)
        return True
    except Exception:
        pass

    # 2) pygame with DirectSound (shared), never WASAPI exclusive
    try:
        import pygame

        os.environ.setdefault("SDL_AUDIODRIVER", "directsound")
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        # Explicit stereo 44.1k is widely compatible with Realtek shared mix
        pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=2048)
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(1.0)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        return True
    except Exception:
        pass

    # 3) last resort: pygame without driver pin
    try:
        import pygame

        try:
            pygame.mixer.quit()
        except Exception:
            pass
        if "SDL_AUDIODRIVER" in os.environ:
            del os.environ["SDL_AUDIODRIVER"]
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(1.0)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        return True
    except Exception:
        return False


def amplify_wav_inplace(wav_path: Path, gain: float = 2.4) -> Path:
    """Boost PCM so MAX98357A / laptop aren't whisper-quiet after mute era."""
    try:
        with wave.open(str(wav_path), "rb") as src:
            nch, sw, fr, nframes, _, _ = src.getparams()[:6]
            frames = src.readframes(nframes)
        if sw != 2 or nframes <= 0:
            return wav_path
        samples = list(struct.unpack("<" + "h" * (len(frames) // 2), frames))
        out = []
        for s in samples:
            v = int(s * gain)
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            out.append(v)
        boosted = wav_path.with_name(wav_path.stem + "_loud.wav")
        with wave.open(str(boosted), "wb") as dst:
            dst.setnchannels(nch)
            dst.setsampwidth(2)
            dst.setframerate(fr)
            dst.writeframes(struct.pack("<" + "h" * len(out), *out))
        return boosted
    except Exception:
        return wav_path


def push_wav_to_esp(
    esp_base: str,
    wav_path: Path,
    serve_host: str = HOST,
    serve_port: int = PORT,
) -> Tuple[bool, str]:
    """Push WAV to ESP amp. play_wav first; play_url fallback with real wait/verify."""
    host = urllib.parse.urlparse(esp_base if "://" in esp_base else f"http://{esp_base}").hostname
    if not host:
        return False, "bad esp host"
    drive = f"http://{host}:8765"
    errors: List[str] = []

    # Restore / nudge volume (no-op on older Trace flash without /api/volume)
    vol_ok = False
    for q in ("level=100", "v=100", "pct=100", "volume=100"):
        try:
            req = urllib.request.Request(f"{drive}/api/volume?{q}", method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if 200 <= resp.status < 300:
                    vol_ok = True
                    break
        except Exception:
            continue
    if not vol_ok:
        for q in ("level=100", "v=100"):
            try:
                urllib.request.urlopen(f"{drive}/api/volume?{q}", timeout=1.2).read()
                vol_ok = True
                break
            except Exception:
                continue

    data = wav_path.read_bytes()
    if len(data) < 44 or data[:4] != b"RIFF":
        return False, "not a wav file"

    # 1) raw play_wav — sync; ESP should ACK after body accepted / play
    try:
        req = urllib.request.Request(
            f"{drive}/api/play_wav",
            data=data,
            method="POST",
            headers={
                "Content-Type": "audio/wav",
                "Content-Length": str(len(data)),
                "Accept": "application/json",
            },
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed = time.time() - t0
            low = body.lower()
            if (
                200 <= resp.status < 300
                and "false" not in low[:80]
                and '"ok":false' not in low.replace(" ", "")
            ):
                return True, f"esp play_wav {len(data)}b ({elapsed:.1f}s) vol={'ok' if vol_ok else 'n/a'}"
            errors.append(f"play_wav HTTP {resp.status} {body[:160]}")
    except Exception as exc:
        err_body = ""
        if hasattr(exc, "read"):
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:160]
            except Exception:
                pass
        errors.append(f"play_wav:{exc} {err_body}".strip())

    # 2) multipart play_wav (peanut-compatible) — some FW only collects upload()
    try:
        boundary = "----TraceWav7MA4YWxkTrZu0gW"
        filename = wav_path.name or "speak.wav"
        pre = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("ascii")
        post = f"\r\n--{boundary}--\r\n".encode("ascii")
        mp = pre + data + post
        req = urllib.request.Request(
            f"{drive}/api/play_wav",
            data=mp,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(mp)),
                "Accept": "application/json",
            },
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed = time.time() - t0
            low = body.lower()
            if (
                200 <= resp.status < 300
                and "false" not in low[:80]
                and '"ok":false' not in low.replace(" ", "")
            ):
                return (
                    True,
                    f"esp play_wav multipart {len(data)}b ({elapsed:.1f}s)",
                )
            errors.append(f"play_wav multipart HTTP {resp.status} {body[:120]}")
    except Exception as exc:
        errors.append(f"play_wav multipart:{exc}")

    # 3) play_url — ESP pulls from laptop LAN; wait for connection close (= play done on current FW)
    rel = wav_path.name
    serve = serve_host
    if serve in ("127.0.0.1", "localhost", "0.0.0.0", "::", "[::]"):
        lan = _guess_lan_ip()
        if lan:
            serve = lan
    public = f"http://{serve}:{serve_port}/_tts_cache/{urllib.parse.quote(rel)}"
    # Ensure cache is reachable from this machine before asking ESP
    try:
        with urllib.request.urlopen(public, timeout=3) as probe:
            got = probe.read(12)
            if got[:4] != b"RIFF":
                errors.append(f"cache not serving WAV at {public}")
                return False, "; ".join(errors)
    except Exception as exc:
        errors.append(f"cache unreachable {public}: {exc}")
        return False, "; ".join(errors)

    try:
        q = urllib.parse.urlencode({"url": public})
        req = urllib.request.Request(
            f"{drive}/api/play_url?{q}",
            method="POST",
            data=b"",
            headers={"Accept": "application/json", "Content-Length": "0"},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed = time.time() - t0
            low = body.lower().replace(" ", "")
            approx_secs = max(0.6, (len(data) - 44) / (16000.0 * 2.0))
            if resp.status >= 300 or '"ok":false' in low or '"played":false' in low:
                errors.append(f"play_url HTTP {resp.status} body={body[:120]} elapsed={elapsed:.1f}s")
            elif '"played":true' in low:
                # New FW — real play completed before ACK
                return True, f"esp play_url {public} (played; prior: {errors[0] if errors else 'n/a'})"
            elif 200 <= resp.status < 300:
                # Old FW ACKs {"queued":true} then plays — wait so amp finishes before UI returns
                wait_s = approx_secs + 0.6
                time.sleep(wait_s)
                return True, (
                    f"esp play_url {public} (queued->waited {wait_s:.1f}s after {elapsed:.2f}s ACK; "
                    f"prior: {errors[0] if errors else 'n/a'})"
                )
            else:
                errors.append(f"play_url HTTP {resp.status} body={body[:120]}")
    except Exception as exc:
        errors.append(f"play_url:{exc}")
    return False, "; ".join(errors)


def _guess_lan_ip() -> Optional[str]:
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return None


def _esp_host(esp_base: Optional[str] = None) -> str:
    base = (esp_base or DEFAULT_ESP).strip() or DEFAULT_ESP
    if "://" not in base:
        base = "http://" + base
    host = urllib.parse.urlparse(base).hostname
    return host or "192.168.1.104"


def _esp_drive_base(esp_base: Optional[str] = None) -> str:
    return f"http://{_esp_host(esp_base)}:8765"


def _esp_stream_url(esp_base: Optional[str] = None) -> str:
    return f"http://{_esp_host(esp_base)}:82/stream"


def _esp_capture_urls(esp_base: Optional[str] = None) -> list:
    h = _esp_host(esp_base)
    return [
        f"http://{h}/capture",
        f"http://{h}:80/capture",
        f"http://{h}:8765/capture",
        f"http://{h}:8765/api/capture",
        f"http://{h}/api/capture",
    ]


def proxy_esp_capture(esp_base: Optional[str] = None) -> Tuple[int, bytes, str]:
    """Fetch one JPEG from ESP /capture (preferred) or peel a frame from MJPEG."""
    last_err = "no capture"
    for url in _esp_capture_urls(esp_base):
        try:
            req = urllib.request.Request(url, headers={"Accept": "image/jpeg,*/*", "Connection": "close"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                if body[:2] == b"\xff\xd8" or "jpeg" in (ctype or "").lower():
                    return 200, body, "image/jpeg"
                last_err = f"not jpeg from {url}"
        except Exception as exc:
            last_err = str(exc)
            continue
    # Fallback: pull one JPEG from MJPEG multipart
    try:
        req = urllib.request.Request(
            _esp_stream_url(esp_base),
            headers={"Accept": "*/*", "Connection": "close"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            buf = b""
            while len(buf) < 250000:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    continue
                end = buf.find(b"\xff\xd9", start + 2)
                if end > start:
                    return 200, buf[start : end + 2], "image/jpeg"
    except Exception as exc:
        last_err = f"mjpeg peel: {exc}"
    return 502, json.dumps({"ok": False, "error": last_err}).encode("utf-8"), "application/json"


def proxy_esp_status(esp_base: Optional[str] = None) -> Tuple[int, bytes, str]:
    """Probe ESP :8765/api/status. Prefer the requested host; light fallback only."""
    bases: List[str] = []
    if esp_base:
        bases.append(esp_base)
    else:
        bases.append(DEFAULT_ESP)
        for n in (104, 105, 106, 102, 100):
            bases.append(f"http://192.168.1.{n}")
    seen = set()
    last_err = "unreachable"
    for b in bases:
        h = _esp_host(b)
        if h in seen:
            continue
        seen.add(h)
        url = f"http://{h}:8765/api/status"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                raw = resp.read()
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    data = {"ok": True, "raw": True}
                data.setdefault("ip", h)
                data.setdefault("esp_base", f"http://{h}")
                data.setdefault("stream", f"http://{h}:82/stream")
                data.setdefault("drive", f"http://{h}:8765/api/drive")
                data["proxy"] = True
                return 200, json.dumps(data).encode("utf-8"), "application/json"
        except Exception as exc:
            last_err = str(exc)
            continue
    return (
        502,
        json.dumps({"ok": False, "error": last_err, "esp_default": DEFAULT_ESP}).encode("utf-8"),
        "application/json",
    )


def proxy_esp_drive(query: str, esp_base: Optional[str] = None) -> Tuple[int, bytes, str]:
    """Forward drive query to ESP :8765/api/drive (avoids browser CORS)."""
    qs = urllib.parse.parse_qs(query, keep_blank_values=True)
    # Allow ?esp= override without forwarding it
    override = None
    if "esp" in qs and qs["esp"]:
        override = qs.pop("esp")[0]
        query = urllib.parse.urlencode(
            {k: v[0] if len(v) == 1 else v for k, v in qs.items()}, doseq=True
        )
    target = f"{_esp_drive_base(override or esp_base)}/api/drive"
    if query:
        target = f"{target}?{query}"
    try:
        req = urllib.request.Request(target, method="GET", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=0.6) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "application/json")
            return int(resp.status), body, ctype
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else str(exc).encode("utf-8")
        return int(exc.code), body, "application/json"
    except Exception as exc:
        return 502, json.dumps({"ok": False, "error": str(exc), "url": target}).encode("utf-8"), "application/json"


def handle_talk(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    lower = text.lower()
    reply = ""
    follow_action: Optional[dict] = None

    try:
        from follow_person import parse_human_command  # type: ignore
    except Exception:
        parse_human_command = None  # type: ignore

    target_id = parse_human_command(text) if parse_human_command else None
    music_q = parse_music_command(text) if parse_music_command else None
    if target_id and PERSON_FOLLOWER is not None:
        st = PERSON_FOLLOWER.status()
        if not st.get("running"):
            PERSON_FOLLOWER.start(esp_base=DEFAULT_ESP, target_human=target_id)
        else:
            PERSON_FOLLOWER.set_target_human(target_id)
        follow_action = {"target_human": target_id, "target_id": target_id, "nav": True}
        reply = f"Copy — staying just behind Human {target_id}."
    elif music_q and YT_PLAYER is not None:
        YT_PLAYER.play_async(music_q)
        reply = f"Okay Ollie — playing {music_q}!"
    elif any(w in lower for w in ("nav off", "stop follow", "stop nav", "cancel follow")):
        if PERSON_FOLLOWER is not None and PERSON_FOLLOWER.status().get("running"):
            PERSON_FOLLOWER.stop("voice stop")
            follow_action = {"nav": False}
            reply = "Nav off — motors stopped."
        else:
            reply = "Nav is already idle."
    elif any(w in lower for w in ("nav on", "start follow", "follow me", "start nav", "follow ollie")):
        if PERSON_FOLLOWER is not None:
            PERSON_FOLLOWER.start(esp_base=DEFAULT_ESP, target_human=1)
            follow_action = {"nav": True, "target_human": 1}
            reply = "Nav on — I'll stick with Ollie as Human 1."
        else:
            reply = "Follow module unavailable."
    elif any(w in lower for w in ("hello", "hi", "hey")):
        reply = "Hey Ollie! Trace-E is ready to play."
    elif any(w in lower for w in ("status", "ready")):
        reply = "Systems online · cam · drive · amp · songs."
    elif any(w in lower for w in ("stop", "halt")):
        if PERSON_FOLLOWER is not None and PERSON_FOLLOWER.status().get("running"):
            PERSON_FOLLOWER.stop("voice halt")
            follow_action = {"nav": False, "motors": 0}
        reply = "Copy that — standing by."
    else:
        # Kid-safe cloud chat when keys exist; else short stub
        reply = None
        if COVER_LISTEN is not None:
            try:
                reply = COVER_LISTEN._kid_chat(text)
            except Exception:
                reply = None
        if not reply:
            reply = f"Trace heard: “{text[:80]}”"

    with _talk_lock:
        _last_talk.update({"text": text, "reply": reply, "ts": time.time()})
    out = {
        "ok": True,
        "heard": text,
        "reply": reply,
        "model": "trace-e-kid",
        "chirp": "talk_heard",
    }
    if follow_action:
        out["follow"] = follow_action
        if PERSON_FOLLOWER is not None:
            out["follow_status"] = PERSON_FOLLOWER.status()
    return out


def resolve_sfx_file(situation: str, file: Optional[str] = None, mode: str = "situational") -> Optional[Path]:
    import random

    if file:
        cand = SFX_DIR / Path(file).name
        if cand.exists():
            return cand
    pool = SFX_MAP.get("random" if mode == "random" else situation) or SFX_MAP["random"]
    random.shuffle(pool)
    for name in pool:
        p = SFX_DIR / name
        if p.exists():
            return p
    return None


def handle_chirp(
    situation: str = "random",
    file: Optional[str] = None,
    esp_base: Optional[str] = None,
    mode: Optional[str] = None,
) -> dict:
    """Play a short Trace chirp — prefer ESP amp, else laptop. Default: silent."""
    mode = (mode or CHIRPS_DEFAULT or "off").strip().lower()
    if mode not in ("situational", "random"):
        return {"ok": True, "skipped": True, "reason": "chirps off"}
    path = resolve_sfx_file(situation, file, mode)
    if not path:
        return {"ok": False, "error": "no sfx file", "dir": str(SFX_DIR)}
    esp = discover_esp(esp_base or DEFAULT_ESP)
    ok_esp, detail = push_wav_to_esp(esp, path)
    if ok_esp:
        return {"ok": True, "file": path.name, "played_on": "esp", "esp": esp, "situation": situation}
    if play_laptop(path):
        return {
            "ok": True,
            "file": path.name,
            "played_on": "laptop",
            "esp": esp,
            "detail": detail,
            "situation": situation,
        }
    return {"ok": False, "error": "play failed", "detail": detail, "file": path.name}


def _normalize_engine(name: Optional[str]) -> str:
    e = (name or DEFAULT_TTS_ENGINE or "peanut-auto").strip().lower()
    aliases = {
        "auto": "peanut-auto",
        "peanut": "peanut-auto",
        "ana": "ana",
        "edge": "ana",
        "edge-tts": "ana",
        "peanut-ana": "ana",
        "groq": "groq",
        "gemini": "gemini",
        "google": "gemini",
        "spidey": "spidey",
        "101": "spidey",
        "101soundboards": "spidey",
        "pyttsx3": "pyttsx3",
        "robot": "pyttsx3",
    }
    return aliases.get(e, e)


def _peanut_engine_chain(force: str) -> List[str]:
    """Ordered TTS engines. Peanut Ana + dual API keys first; Spidey optional; pyttsx3 last."""
    force = _normalize_engine(force)
    if force in ("ana", "groq", "gemini", "spidey", "pyttsx3"):
        # Still allow soft fallbacks after the forced primary
        rest = [x for x in ("ana", "groq", "gemini", "spidey", "pyttsx3") if x != force]
        if force == "spidey":
            return [force] + [x for x in rest if x != "pyttsx3"] + ["pyttsx3"]
        return [force] + rest
    # peanut-auto: true Ana voice, then Groq, then Gemini, optional Spidey, pyttsx3
    chain = ["ana", "groq", "gemini"]
    if SPIDEY_TTS_ENABLED:
        chain.append("spidey")
    chain.append("pyttsx3")
    return chain


def _synth_by_engine(engine: str, text: str) -> Tuple[Optional[Path], Optional[str]]:
    """Return (wav_path, error)."""
    try:
        if engine == "ana":
            return synth_edge_ana_wav(text), None
        if engine == "groq":
            return synth_groq_tts_wav(text), None
        if engine == "gemini":
            return synth_gemini_tts_wav(text), None
        if engine == "spidey":
            mp3 = try_101_spidey_tts(text)
            if not mp3:
                return None, "101soundboards unavailable"
            wav = mp3_to_wav_16k(mp3) or ensure_wav_16k_mono(mp3)
            if not wav:
                return None, "spidey mp3->wav failed"
            return wav, None
        if engine == "pyttsx3":
            return synth_pyttsx3_wav(text), None
        return None, f"unknown engine {engine}"
    except Exception as exc:
        return None, str(exc)


def handle_speak(
    text: str,
    esp_base: Optional[str] = None,
    engine: Optional[str] = None,
    allow_laptop: Optional[bool] = None,
) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    # Amp-first / amp-only — laptop only when explicitly opted in
    use_laptop = ALLOW_LAPTOP if allow_laptop is None else bool(allow_laptop)
    esp = discover_esp(esp_base or DEFAULT_ESP)
    want = _normalize_engine(engine)
    chain = _peanut_engine_chain(want)
    errors: Dict[str, str] = {}
    wav: Optional[Path] = None
    used = None

    for eng in chain:
        path, err = _synth_by_engine(eng, text)
        if path and path.exists() and path.stat().st_size > 44:
            wav = path
            used = eng if eng != "ana" else "peanut-ana/edge-tts"
            if eng == "groq":
                used = f"groq/{GROQ_TTS_VOICE}"
            elif eng == "gemini":
                used = f"gemini/{GEMINI_TTS_VOICE}"
            elif eng == "spidey":
                used = "101soundboards"
            break
        errors[eng] = err or "failed"

    if wav is None:
        return {
            "ok": False,
            "error": "tts failed (all engines)",
            "tried": chain,
            "errors": errors,
            "keys": _key_status(),
        }

    # Loudness restore — beep-kill era left peanut vol=0; Trace may lack /api/volume until flash
    wav = amplify_wav_inplace(wav, gain=2.6)
    # Ensure amplified file lives under _tts_cache for ESP play_url
    if wav.parent != CACHE_DIR:
        dest = CACHE_DIR / wav.name
        try:
            dest.write_bytes(wav.read_bytes())
            wav = dest
        except Exception:
            pass

    ok_esp, detail = push_wav_to_esp(esp, wav)
    if ok_esp:
        return {
            "ok": True,
            "text": text,
            "engine": used,
            "requested_engine": want,
            "played_on": "esp",
            "status": "Speaking on amp…",
            "esp": esp,
            "wav": str(wav.name),
            "detail": detail,
            "errors": errors or None,
            "keys": {
                "groq": bool(GROQ_API_KEY),
                "gemini": bool(GEMINI_API_KEY),
            },
            "allow_laptop": use_laptop,
            "volume_note": "wav gain×2.6; /api/volume nudged if present",
        }

    if use_laptop and play_laptop(wav):
        return {
            "ok": True,
            "text": text,
            "engine": used,
            "requested_engine": want,
            "played_on": "laptop",
            "status": "Speaking on laptop… (amp failed — opt-in fallback)",
            "esp": esp,
            "wav": str(wav.name),
            "detail": f"esp failed ({detail})",
            "errors": errors or None,
            "keys": {
                "groq": bool(GROQ_API_KEY),
                "gemini": bool(GEMINI_API_KEY),
            },
            "allow_laptop": True,
            "volume_note": "laptop fallback only — set allow_laptop/TRACE_E_ALLOW_LAPTOP",
        }

    return {
        "ok": False,
        "error": "amp play failed"
        + ("" if use_laptop else " (laptop fallback disabled — pass allow_laptop:true to opt in)"),
        "esp": esp,
        "engine": used,
        "detail": detail,
        "errors": errors,
        "allow_laptop": use_laptop,
    }


class TraceHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DESKTOP_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[speak] " + (fmt % args) + "\n")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = parsed.query
        q = urllib.parse.parse_qs(qs, keep_blank_values=True)

        if path in ("/", "/index.html", "/web-quarters", "/mock"):
            data = (DESKTOP_DIR / "mock_ui.html").read_bytes()
            self._send(200, data, "text/html; charset=utf-8")
            return
        if path == "/api/health":
            code, body, ctype = _json_bytes(
                {
                    "ok": True,
                    "service": "trace-e-speak",
                    "esp_default": DEFAULT_ESP,
                    "port": PORT,
                    "proxies": ["/api/esp/stream", "/api/esp/drive", "/api/esp/status"],
                    "follow": {
                        "available": PERSON_FOLLOWER is not None,
                        "status": (PERSON_FOLLOWER.status() if PERSON_FOLLOWER else None),
                        "endpoints": [
                            "/api/follow/start",
                            "/api/follow/stop",
                            "/api/follow/target",
                            "/api/follow/status",
                            "/api/follow/frame",
                        ],
                    },
                    "listen": (
                        COVER_LISTEN.status() if COVER_LISTEN is not None else {"enabled": False}
                    ),
                    "music": (
                        YT_PLAYER.status() if YT_PLAYER is not None else {"enabled": False}
                    ),
                    "cam_hub": (CAM_HUB.status() if CAM_HUB else None),
                    "sfx": sorted(p.name for p in SFX_DIR.glob("*.wav")),
                    "tts": {
                        "default": DEFAULT_TTS_ENGINE,
                        "engines": [
                            "peanut-auto",
                            "ana",
                            "groq",
                            "gemini",
                            "spidey",
                            "pyttsx3",
                        ],
                        "primary": "peanut-ana (edge-tts) -> groq -> gemini",
                        "keys": _key_status(),
                    },
                    "chirps": CHIRPS_DEFAULT,
                    "playback": {
                        "primary": "esp-amp",
                        "allow_laptop_default": ALLOW_LAPTOP,
                        "esp_default": DEFAULT_ESP,
                    },
                }
            )
            self._send(code, body, ctype)
            return
        if path == "/api/talk/last":
            with _talk_lock:
                payload = dict(_last_talk)
            code, body, ctype = _json_bytes({"ok": True, **payload})
            self._send(code, body, ctype)
            return

        if path in ("/api/listen/status", "/api/listen"):
            if COVER_LISTEN is None:
                code, body, ctype = _json_bytes({"ok": False, "enabled": False}, 503)
            else:
                code, body, ctype = _json_bytes({"ok": True, **COVER_LISTEN.status()})
            self._send(code, body, ctype)
            return

        if path in ("/api/music/status", "/api/music"):
            if YT_PLAYER is None:
                code, body, ctype = _json_bytes({"ok": False, "enabled": False}, 503)
            else:
                code, body, ctype = _json_bytes({"ok": True, "enabled": True, **YT_PLAYER.status()})
            self._send(code, body, ctype)
            return

        if path in ("/api/status", "/api/esp/status", "/api/esp/discover"):
            esp = (q.get("esp") or q.get("esp_base") or [None])[0]
            code, body, ctype = proxy_esp_status(esp)
            self._send(code, body, ctype)
            return

        if path == "/api/esp/drive":
            esp = (q.get("esp") or q.get("esp_base") or [None])[0]
            code, body, ctype = proxy_esp_drive(qs, esp)
            self._send(code, body, ctype)
            return

        if path in ("/api/follow/status", "/api/follow"):
            if PERSON_FOLLOWER is None:
                code, body, ctype = _json_bytes(
                    {"ok": False, "error": "follow module missing (opencv?)"}, 503
                )
            else:
                code, body, ctype = _json_bytes(PERSON_FOLLOWER.status())
            self._send(code, body, ctype)
            return

        if path == "/api/follow/frame":
            if PERSON_FOLLOWER is None:
                self._send(503, b'{"ok":false,"error":"follow unavailable"}', "application/json")
                return
            jpg = PERSON_FOLLOWER.annotated_jpeg()
            if not jpg:
                jpg, _ = PERSON_FOLLOWER.wait_annotated(timeout=0.6, after_seq=-1)
            if not jpg and CAM_HUB is not None:
                jpg = CAM_HUB.latest_jpeg()
            if not jpg:
                self._send(404, b'{"ok":false,"error":"no frame yet"}', "application/json")
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(jpg)))
            self.end_headers()
            self.wfile.write(jpg)
            return

        if path in ("/api/cam/latest", "/api/esp/latest"):
            esp = (q.get("esp") or q.get("esp_base") or [None])[0]
            if CAM_HUB is None:
                self._send(503, b'{"ok":false,"error":"cam hub missing"}', "application/json")
                return
            CAM_HUB.ensure(esp or DEFAULT_ESP)
            # Prefer annotated frame while following
            jpg = None
            if PERSON_FOLLOWER is not None:
                st = PERSON_FOLLOWER.status()
                if st.get("running"):
                    jpg = PERSON_FOLLOWER.annotated_jpeg()
            if not jpg:
                jpg = CAM_HUB.latest_jpeg()
            if not jpg:
                err = CAM_HUB.status().get("error") or "no frame"
                self._send(
                    503,
                    json.dumps({"ok": False, "error": err}).encode("utf-8"),
                    "application/json",
                )
                return
            self._send(200, jpg, "image/jpeg")
            return

        if path in ("/api/esp/stream", "/api/cam/stream", "/api/cam/mjpeg"):
            esp = (q.get("esp") or q.get("esp_base") or [None])[0]
            if CAM_HUB is None:
                # Legacy direct proxy fallback
                url = _esp_stream_url(esp)
                try:
                    req = urllib.request.Request(
                        url,
                        headers={
                            "User-Agent": _UA,
                            "Accept": "*/*",
                            "Connection": "close",
                            "Accept-Encoding": "identity",
                        },
                    )
                    upstream = urllib.request.urlopen(req, timeout=8)
                except Exception as exc:
                    msg = json.dumps({"ok": False, "error": str(exc), "url": url}).encode("utf-8")
                    self._send(502, msg, "application/json")
                    return
                try:
                    ctype = upstream.headers.get(
                        "Content-Type", "multipart/x-mixed-replace; boundary=frame"
                    )
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = upstream.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        try:
                            self.wfile.flush()
                        except Exception:
                            break
                finally:
                    try:
                        upstream.close()
                    except Exception:
                        pass
                return

            # Shared hub MJPEG — never opens a second ESP :82 socket
            CAM_HUB.ensure(esp or DEFAULT_ESP)
            force_annot = (q.get("annotate") or q.get("nav") or ["0"])[0] in (
                "1",
                "true",
                "yes",
                "on",
            )
            boundary = b"frame"
            self.send_response(200)
            self._cors()
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=" + boundary.decode()
            )
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            seq = -1
            annot_seq = -1
            try:
                while True:
                    # While nav/follow is ON, push annotated frames (all boxes + HUD)
                    jpg = None
                    following = False
                    if PERSON_FOLLOWER is not None:
                        st = PERSON_FOLLOWER.status()
                        following = bool(st.get("running"))
                    if following or force_annot:
                        if PERSON_FOLLOWER is not None and following:
                            # Wait for a fresh annotated frame while nav is live
                            jpg, annot_seq = PERSON_FOLLOWER.wait_annotated(
                                timeout=0.85, after_seq=annot_seq
                            )
                        elif PERSON_FOLLOWER is not None:
                            # annotate=1 but nav not running yet — never block 0.85s
                            jpg = PERSON_FOLLOWER.annotated_jpeg()
                            if jpg is not None:
                                annot_seq = int(st.get("annot_seq") or annot_seq)
                        if jpg is None and CAM_HUB is not None:
                            jpg, seq = CAM_HUB.wait_jpeg(timeout=0.4, after_seq=seq)
                    else:
                        jpg, seq = CAM_HUB.wait_jpeg(timeout=1.0, after_seq=seq)
                    if not jpg:
                        continue
                    header = (
                        b"--" + boundary + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpg)).encode() + b"\r\n"
                        b"Cache-Control: no-store\r\n\r\n"
                    )
                    self.wfile.write(header)
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    try:
                        self.wfile.flush()
                    except Exception:
                        break
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:
                pass
            return

        if path in ("/api/esp/capture", "/api/cam/capture"):
            esp = (q.get("esp") or q.get("esp_base") or [None])[0]
            code, body, ctype = proxy_esp_capture(esp)
            self._send(code, body, ctype)
            return

        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = parsed.query
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        # Also accept form
        if not data and raw:
            try:
                data = dict(urllib.parse.parse_qsl(raw.decode("utf-8")))
            except Exception:
                data = {}

        if path == "/api/esp/drive":
            # Merge JSON/form body into query for peanut/trace drive APIs
            q = dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))
            for k in ("throttle", "steer", "turn", "speed", "left", "right", "l", "r", "cmd", "esp", "esp_base"):
                if k in data and data[k] is not None and k not in q:
                    q[k] = str(data[k])
            esp = q.pop("esp", None) or q.pop("esp_base", None) or data.get("esp") or data.get("esp_base")
            code, body, ctype = proxy_esp_drive(urllib.parse.urlencode(q), esp)
            self._send(code, body, ctype)
            return

        if path == "/api/talk":
            result = handle_talk(str(data.get("text") or data.get("message") or ""))
            if result.get("ok") and (
                data.get("speak")
                or str(data.get("say_reply") or "").lower() in ("1", "true", "yes")
            ):
                try:
                    handle_speak(str(result.get("reply") or ""), DEFAULT_ESP)
                except Exception:
                    pass
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 400)
            self._send(code, body, ctype)
            return

        if path in ("/api/listen/start", "/api/listen/trigger"):
            if COVER_LISTEN is None:
                result = {"ok": False, "error": "listen module missing"}
            else:
                result = COVER_LISTEN.trigger_manual()
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 400)
            self._send(code, body, ctype)
            return

        if path in ("/api/listen/audio", "/api/listen/upload"):
            if COVER_LISTEN is None:
                result = {"ok": False, "error": "listen module missing"}
            else:
                ctype_in = (
                    self.headers.get("Content-Type") or "application/octet-stream"
                ).split(";")[0].strip()
                audio = b""
                if raw and not (raw[:1] in (b"{", b"[") and "json" in ctype_in):
                    audio = raw
                elif isinstance(data.get("audio_b64"), str):
                    import base64

                    try:
                        audio = base64.b64decode(data["audio_b64"])
                        ctype_in = str(data.get("mime") or "audio/webm")
                    except Exception:
                        audio = b""
                result = COVER_LISTEN.submit_audio(audio, ctype_in)
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 400)
            self._send(code, body, ctype)
            return

        if path in ("/api/music/play", "/api/music"):
            if YT_PLAYER is None:
                result = {"ok": False, "error": "music module missing (yt-dlp?)"}
            else:
                q = str(
                    data.get("query")
                    or data.get("q")
                    or data.get("song")
                    or data.get("text")
                    or ""
                ).strip()
                if not q and parse_music_command:
                    q = parse_music_command(str(data.get("command") or "")) or ""
                result = YT_PLAYER.play_async(q)
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 400)
            self._send(code, body, ctype)
            return

        if path == "/api/chirp":
            result = handle_chirp(
                situation=str(data.get("situation") or "random"),
                file=data.get("file"),
                esp_base=data.get("esp") or data.get("esp_base"),
                mode=str(data.get("mode") or CHIRPS_DEFAULT or "off"),
            )
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 500)
            self._send(code, body, ctype)
            return

        if path in ("/api/speak", "/api/say"):
            esp = data.get("esp") or data.get("esp_base")
            engine = data.get("engine") or data.get("voice") or data.get("tts")
            allow_raw = data.get("allow_laptop")
            if allow_raw is None:
                allow_raw = data.get("laptop")
            allow_laptop: Optional[bool]
            if allow_raw is None:
                allow_laptop = None
            elif isinstance(allow_raw, bool):
                allow_laptop = allow_raw
            else:
                allow_laptop = str(allow_raw).strip().lower() in ("1", "true", "yes", "on")
            try:
                result = handle_speak(
                    str(data.get("text") or data.get("say") or ""),
                    esp,
                    engine=str(engine) if engine else None,
                    allow_laptop=allow_laptop,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "trace": traceback.format_exc()[-400:]}
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 500)
            self._send(code, body, ctype)
            return

        if path in ("/api/follow/start", "/api/follow/on"):
            if PERSON_FOLLOWER is None:
                result = {"ok": False, "error": "follow module missing"}
            else:
                esp = data.get("esp") or data.get("esp_base") or DEFAULT_ESP
                tid = data.get("target_human")
                if tid is None:
                    tid = data.get("target_id") or data.get("human") or data.get("target") or data.get("person")
                if tid is None and (data.get("text") or data.get("command")):
                    try:
                        from follow_person import parse_human_command

                        tid = parse_human_command(str(data.get("text") or data.get("command")))
                    except Exception:
                        tid = None
                try:
                    kw: Dict[str, Any] = {
                        "esp_base": str(esp),
                        "forward_fast": data.get("forward_fast"),
                        "turn_max": data.get("turn_max"),
                        "mirror_x": data.get("mirror_x"),
                    }
                    if tid is not None:
                        kw["target_human"] = int(tid)
                    result = PERSON_FOLLOWER.start(**kw)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 500)
            self._send(code, body, ctype)
            return

        if path in ("/api/follow/target", "/api/follow/human"):
            if PERSON_FOLLOWER is None:
                result = {"ok": False, "error": "follow module missing"}
            else:
                tid = data.get("target_human")
                if tid is None:
                    tid = data.get("target_id") or data.get("human") or data.get("target") or data.get("person")
                if tid is None and (data.get("text") or data.get("command")):
                    try:
                        from follow_person import parse_human_command

                        tid = parse_human_command(str(data.get("text") or data.get("command")))
                    except Exception:
                        tid = None
                try:
                    if tid is None:
                        result = {"ok": False, "error": "need target_human / human N"}
                    else:
                        if not PERSON_FOLLOWER.status().get("running"):
                            PERSON_FOLLOWER.start(
                                esp_base=str(data.get("esp") or data.get("esp_base") or DEFAULT_ESP),
                                target_human=int(tid),
                            )
                        result = PERSON_FOLLOWER.set_target_human(int(tid))
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 500)
            self._send(code, body, ctype)
            return

        if path in ("/api/follow/stop", "/api/follow/off"):
            if PERSON_FOLLOWER is None:
                result = {"ok": False, "error": "follow module missing"}
            else:
                reason = str(data.get("reason") or "ui stop")
                try:
                    result = PERSON_FOLLOWER.stop(reason)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
            code, body, ctype = _json_bytes(result, 200 if result.get("ok") else 500)
            self._send(code, body, ctype)
            return

        self._send(404, b'{"ok":false,"error":"not found"}', "application/json")


def main() -> int:
    os.chdir(DESKTOP_DIR)
    httpd = ThreadingHTTPServer((HOST, PORT), TraceHandler)
    print(f"Trace-E Web-Quarters  http://{HOST}:{PORT}/", flush=True)
    print(f"Talk TO Trace         POST /api/talk", flush=True)
    print(f"Talk THROUGH Trace    POST /api/speak  (engine={DEFAULT_TTS_ENGINE})", flush=True)
    print(
        "TTS Peanut primary    Ana/edge-tts -> Groq -> Gemini -> Spidey? -> pyttsx3",
        flush=True,
    )
    print(
        f"API keys              groq={'yes' if GROQ_API_KEY else 'no'}  "
        f"gemini={'yes' if GEMINI_API_KEY else 'no'}  dotenv={len(_DOTENV_LOADED)} file(s)",
        flush=True,
    )
    print(f"Chirps                POST /api/chirp (default={CHIRPS_DEFAULT})", flush=True)
    print(
        f"Playback              amp-first · laptop_fallback={'ON' if ALLOW_LAPTOP else 'OFF (pass allow_laptop)'}",
        flush=True,
    )
    print(f"ESP proxy             GET  /api/esp/stream  /api/esp/drive  /api/esp/status", flush=True)
    print(
        f"Person follow         POST /api/follow/start|stop  GET /api/follow/status|frame  "
        f"({'ready' if PERSON_FOLLOWER else 'UNAVAILABLE'})",
        flush=True,
    )
    print(
        f"Cam hub               GET  /api/esp/stream (shared · one ESP :82 socket)  "
        f"({'ready' if CAM_HUB else 'UNAVAILABLE'})",
        flush=True,
    )
    print(f"ESP default           {DEFAULT_ESP}", flush=True)
    print(f"SFX dir               {SFX_DIR}", flush=True)
    if CAM_HUB is not None:
        try:
            CAM_HUB.ensure(DEFAULT_ESP)
            print(f"Cam hub warming       {DEFAULT_ESP}:82/stream", flush=True)
        except Exception as exc:
            print(f"Cam hub warm failed   {exc}", flush=True)
    if COVER_LISTEN is not None:
        try:
            COVER_LISTEN.attach(handle_speak, PERSON_FOLLOWER)
            COVER_LISTEN.start(DEFAULT_ESP)
            print(
                "Cover-listen          cover HC-SR04 -> Listening! -> Whisper/Groq -> TTS",
                flush=True,
            )
        except Exception as exc:
            print(f"Cover-listen failed   {exc}", flush=True)
    if YT_PLAYER is not None:
        try:
            YT_PLAYER.attach(
                esp=DEFAULT_ESP,
                speak_fn=handle_speak,
                play_wav_fn=push_wav_to_esp,
            )
            print("Music                 POST /api/music/play  (yt-dlp -> amp)", flush=True)
        except Exception as exc:
            print(f"Music attach failed   {exc}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
