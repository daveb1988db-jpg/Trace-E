#!/usr/bin/env python3
"""
Trace-E Web-Quarters local brain — serve mock_ui + Talk APIs.

Endpoints:
  GET  /                  → mock_ui.html
  GET  /api/health
  POST /api/talk          → Talk TO Trace (chat/command stub)
  POST /api/speak         → Talk THROUGH Trace (TTS → ESP amp, laptop fallback)

TTS (Peanut Ana PRIMARY for Ollie):
  1) edge-tts en-US-AnaNeural  (Peanut's working Ana voice)
  2) Groq Orpheus TTS          (GROQ_API_KEY)
  3) Gemini TTS                (GEMINI_API_KEY / GOOGLE_API_KEY)
  4) optional 101Soundboards Spidey
  5) pyttsx3 only as last resort

Playback: prefer ESP :8765 /api/play_wav or /api/play_url; else laptop pygame/winsound.
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

# Load API keys from Trace-E .env and/or peanut-robot .env (never commit real .env)
def _load_dotenv_files() -> List[str]:
    loaded: List[str] = []
    try:
        from dotenv import load_dotenv
    except Exception:
        return loaded
    peanut_root = Path(os.environ.get("PEANUT_ROOT") or r"C:\Users\Bartl\Documents\peanut-robot")
    candidates = [
        REPO_DIR / ".env",
        DESKTOP_DIR / ".env",
        peanut_root / ".env",
    ]
    for path in candidates:
        try:
            if path.is_file():
                load_dotenv(path, override=False)
                loaded.append(str(path))
        except Exception:
            continue
    return loaded


_DOTENV_LOADED = _load_dotenv_files()

# Situation → candidate WAV filenames (replace files in assets/sfx/ with real Spidey clips)
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
DEFAULT_ESP = (
    os.environ.get("TRACE_E_ESP_BASE")
    or os.environ.get("ESP_BASE")
    or os.environ.get("ESP_BASE_URL")
    or "http://192.168.1.104"
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
    for base in (preferred, DEFAULT_ESP, "http://192.168.1.104", "http://192.168.1.105"):
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
    """Peanut Ana voice via edge-tts (en-US-AnaNeural) → 16 kHz mono WAV."""
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
    """Groq Orpheus/PlayAI TTS → 16 kHz mono WAV (needs GROQ_API_KEY)."""
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
    """Gemini native TTS → 16 kHz mono WAV (needs GEMINI_API_KEY / GOOGLE_API_KEY)."""
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
    """Best-effort 101Soundboards Spidey TTS → cached mp3. Returns None on CF/block/fail."""
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
    """Peanut-style local robot voice → 16-bit mono WAV @ 16 kHz."""
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
    """Convert mp3→wav via pydub/ffmpeg if available; else None."""
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
    try:
        import pygame

        pygame.mixer.init()
        pygame.mixer.music.load(str(wav_path))
        pygame.mixer.music.set_volume(1.0)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        return True
    except Exception:
        pass
    try:
        import winsound

        winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
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


def push_wav_to_esp(esp_base: str, wav_path: Path, serve_host: str = HOST, serve_port: int = PORT) -> Tuple[bool, str]:
    """Push WAV to ESP amp. Prefer sync play_wav (real result); play_url is fire-and-forget."""
    host = urllib.parse.urlparse(esp_base if "://" in esp_base else f"http://{esp_base}").hostname
    if not host:
        return False, "bad esp host"
    drive = f"http://{host}:8765"

    # Restore / nudge volume if peanut-style /api/volume exists (no-op on Trace)
    for q in ("level=100", "v=100", "pct=100", "volume=100"):
        try:
            urllib.request.urlopen(f"{drive}/api/volume?{q}", timeout=1.2).read()
            break
        except Exception:
            continue

    # 1) raw play_wav — synchronous; ESP only ACKs after body accepted / play starts
    try:
        data = wav_path.read_bytes()
        req = urllib.request.Request(
            f"{drive}/api/play_wav",
            data=data,
            method="POST",
            headers={"Content-Type": "audio/wav", "Content-Length": str(len(data))},
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if 200 <= resp.status < 300 and "false" not in body.lower()[:40]:
                return True, f"esp play_wav {len(data)}b"
            play_wav_err = f"play_wav HTTP {resp.status} {body[:120]}"
    except Exception as exc:
        play_wav_err = str(exc)

    # 2) play_url fallback (ESP pulls from laptop LAN)
    rel = wav_path.name
    serve = serve_host
    if serve in ("127.0.0.1", "localhost", "0.0.0.0", "::", "[::]"):
        lan = _guess_lan_ip()
        if lan:
            serve = lan
    public = f"http://{serve}:{serve_port}/_tts_cache/{rel}"
    try:
        q = urllib.parse.urlencode({"url": public})
        req = urllib.request.Request(f"{drive}/api/play_url?{q}", method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=12) as resp:
            if 200 <= resp.status < 300:
                # Give ESP time to pull+play; cannot know success — treat as tentative
                time.sleep(0.4)
                return True, f"esp play_url {public} (after play_wav fail: {play_wav_err})"
    except Exception as exc:
        return False, f"play_wav:{play_wav_err}; play_url:{exc}"
    return False, f"play_wav:{play_wav_err}; play_url rejected"


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
    """Probe ESP :8765/api/status (and fall back to discover)."""
    bases = []
    if esp_base:
        bases.append(esp_base)
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
            with urllib.request.urlopen(req, timeout=1.5) as resp:
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
        with urllib.request.urlopen(req, timeout=2.5) as resp:
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
    # Stub reply — ready for future LLM / command router
    lower = text.lower()
    if any(w in lower for w in ("hello", "hi", "hey")):
        reply = "Webs up! Trace-E heard you."
    elif any(w in lower for w in ("status", "ready")):
        reply = "Systems online · cam · drive · amp."
    elif any(w in lower for w in ("stop", "halt")):
        reply = "Copy that — standing by."
    else:
        reply = f"Trace heard: “{text[:80]}”"
    with _talk_lock:
        _last_talk.update({"text": text, "reply": reply, "ts": time.time()})
    return {
        "ok": True,
        "heard": text,
        "reply": reply,
        "model": "trace-e",
        "chirp": "talk_heard",
    }


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
) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
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

    # Loudness restore — beep-kill era left peanut vol=0; Trace has no volume API
    wav = amplify_wav_inplace(wav, gain=2.6)

    played_on = None
    detail = ""
    ok_esp, detail = push_wav_to_esp(esp, wav)
    if ok_esp:
        played_on = "esp"
        if "play_url" in detail and "play_wav" in detail:
            if play_laptop(wav):
                played_on = "laptop+esp?"
                detail = detail + "; mirrored to laptop (play_url untrusted)"
    else:
        if play_laptop(wav):
            played_on = "laptop"
            detail = f"esp failed ({detail})"
        else:
            return {
                "ok": False,
                "error": "could not play on ESP or laptop",
                "esp": esp,
                "engine": used,
                "detail": detail,
                "errors": errors,
            }

    return {
        "ok": True,
        "text": text,
        "engine": used,
        "requested_engine": want,
        "played_on": played_on,
        "esp": esp,
        "wav": str(wav.name),
        "detail": detail,
        "errors": errors or None,
        "keys": {
            "groq": bool(GROQ_API_KEY),
            "gemini": bool(GEMINI_API_KEY),
        },
        "volume_note": "wav gain×2.6; Trace has no /api/volume (peanut mute N/A)",
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

        if path in ("/api/esp/status", "/api/esp/discover"):
            esp = (q.get("esp") or q.get("esp_base") or [None])[0]
            code, body, ctype = proxy_esp_status(esp)
            self._send(code, body, ctype)
            return

        if path == "/api/esp/drive":
            esp = (q.get("esp") or q.get("esp_base") or [None])[0]
            code, body, ctype = proxy_esp_drive(qs, esp)
            self._send(code, body, ctype)
            return

        if path in ("/api/esp/stream", "/api/cam/stream"):
            esp = (q.get("esp") or q.get("esp_base") or [None])[0]
            url = _esp_stream_url(esp)
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": _UA, "Accept": "*/*", "Connection": "close"},
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
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = upstream.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    try:
                        self.wfile.flush()
                    except Exception:
                        break
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:
                pass
            finally:
                try:
                    upstream.close()
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
            try:
                result = handle_speak(
                    str(data.get("text") or data.get("say") or ""),
                    esp,
                    engine=str(engine) if engine else None,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "trace": traceback.format_exc()[-400:]}
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
    print(f"ESP proxy             GET  /api/esp/stream  /api/esp/drive  /api/esp/status", flush=True)
    print(f"ESP default           {DEFAULT_ESP}", flush=True)
    print(f"SFX dir               {SFX_DIR}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
