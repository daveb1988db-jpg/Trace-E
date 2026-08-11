#!/usr/bin/env python3
"""
YouTube → short kid-safe audio clip → ESP amp (via existing play_wav).

Uses open-source yt-dlp + bundled ffmpeg (imageio-ffmpeg). Async so UI stays snappy.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

DESKTOP = Path(__file__).resolve().parent
CACHE = DESKTOP / "_yt_cache"
CACHE.mkdir(exist_ok=True)

MAX_SECONDS = 75
KIDS_SUFFIX = "kids nursery rhyme official audio"

_block = re.compile(
    r"\b(nsfw|explicit|18\+|porn|sex|kill|murder|gore|hate)\b", re.I
)


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil

        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError("ffmpeg missing")
        return exe


class YoutubePlayer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "phase": "idle",
            "query": "",
            "title": "",
            "message": "Say play a song, or use Music in the UI",
            "last_error": "",
            "updated": 0.0,
        }
        self._speak: Optional[Callable[..., dict]] = None
        self._play_wav: Optional[Callable[..., Any]] = None
        self._esp = "http://192.168.1.104"
        self._busy = False

    def attach(
        self,
        *,
        esp: str,
        speak_fn: Callable[..., dict],
        play_wav_fn: Callable[..., Any],
    ) -> None:
        self._esp = esp.rstrip("/")
        self._speak = speak_fn
        self._play_wav = play_wav_fn

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._status.update(kw)
            self._status["updated"] = time.time()

    def play_async(self, query: str) -> Dict[str, Any]:
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "empty query"}
        if _block.search(q):
            return {"ok": False, "error": "that song request is not allowed"}
        if self._busy:
            return {"ok": False, "error": "already fetching/playing"}
        threading.Thread(
            target=self._run, args=(q,), name="trace-yt", daemon=True
        ).start()
        return {"ok": True, "started": True, "query": q}

    def _run(self, query: str) -> None:
        self._busy = True
        self._set(
            phase="fetch",
            query=query,
            title="",
            message=f"Finding “{query}”…",
            last_error="",
        )
        try:
            if self._speak:
                try:
                    self._speak(f"Okay Ollie, playing {query}!", self._esp)
                except Exception:
                    pass
            wav, title = self._download_wav(query)
            self._set(phase="play", title=title, message=f"Playing {title}")
            if not self._play_wav:
                raise RuntimeError("no amp play function")
            ok, detail = self._play_wav(self._esp, wav)
            if not ok:
                raise RuntimeError(detail or "amp play failed")
            self._set(phase="idle", message=f"Played: {title}")
        except Exception as exc:
            self._set(phase="idle", last_error=str(exc), message="Music failed")
            if self._speak:
                try:
                    self._speak("Sorry, I could not play that song.", self._esp)
                except Exception:
                    pass
        finally:
            self._busy = False

    def _download_wav(self, query: str) -> Tuple[Path, str]:
        import yt_dlp

        key = hashlib.sha1(query.lower().encode("utf-8")).hexdigest()[:16]
        out_base = CACHE / f"yt_{key}"
        wav_path = out_base.with_suffix(".wav")
        meta_path = out_base.with_suffix(".json")
        if wav_path.is_file() and wav_path.stat().st_size > 2000:
            title = query
            if meta_path.is_file():
                try:
                    title = json.loads(meta_path.read_text(encoding="utf-8")).get(
                        "title"
                    ) or query
                except Exception:
                    pass
            return wav_path, title

        if re.search(r"youtube\.com|youtu\.be", query, re.I):
            search = query
        else:
            search = f"ytsearch1:{query} {KIDS_SUFFIX}"

        ydl_opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "outtmpl": str(CACHE / f"yt_{key}_raw.%(ext)s"),
        }
        title = query
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search, download=True)
            if info and "entries" in info:
                info = (info.get("entries") or [None])[0]
            if not info:
                raise RuntimeError("no YouTube result")
            title = str(info.get("title") or query)
            if _block.search(title):
                raise RuntimeError("that video is not allowed")

        raw = None
        for p in CACHE.glob(f"yt_{key}_raw.*"):
            if p.suffix.lower() in (".m4a", ".webm", ".opus", ".mp3", ".ogg", ".wav"):
                raw = p
                break
        if raw is None:
            raise RuntimeError("download missing")

        ff = _ffmpeg()
        subprocess.run(
            [
                ff,
                "-y",
                "-i",
                str(raw),
                "-t",
                str(MAX_SECONDS),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-sample_fmt",
                "s16",
                str(wav_path),
            ],
            check=True,
            capture_output=True,
        )
        try:
            raw.unlink()
        except Exception:
            pass
        meta_path.write_text(
            json.dumps({"title": title, "query": query}), encoding="utf-8"
        )
        if not wav_path.is_file():
            raise RuntimeError("wav not produced")
        return wav_path, title


YT_PLAYER = YoutubePlayer()


def parse_music_command(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    m = re.search(
        r"\b(?:play|put on)\s+(?:(?:the|a|some)\s+)?(?:song|music|tune|track)\s+(.+)$",
        low,
    )
    if m:
        return m.group(1).strip(" .!?")
    m = re.search(r"\bplay\s+(?:me\s+)?(.+?)(?:\s+please)?$", low)
    if m and any(w in low for w in ("song", "music", "youtube", "nursery", "rhyme")):
        q = m.group(1).strip(" .!?")
        q = re.sub(r"\b(song|music|on youtube|please)\b", "", q).strip()
        return q or None
    if "youtube.com" in low or "youtu.be" in low:
        return t
    return None
