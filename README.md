# Trace-E Bot

## 📱 Download the kids-tablet app (APK)

> **[⬇ Download the latest Trace-E.apk](https://github.com/daveb1988db-jpg/Trace-E/releases/latest/download/Trace-E.apk)**  ·  [All releases](https://github.com/daveb1988db-jpg/Trace-E/releases/latest)

[![Latest APK](https://img.shields.io/github/v/release/daveb1988db-jpg/Trace-E?label=Download%20APK&style=for-the-badge&logo=android&color=2ea44f)](https://github.com/daveb1988db-jpg/Trace-E/releases/latest)

Sideload on the tablet (Settings → allow **install unknown apps** for your browser/file manager), then open **WEB-QUARTERS!**.

**Wide-screen kids cockpit:** tap **🕹️ WIDE DRIVE** to flip into a full-screen landscape view with a big camera and an **analog thumb-stick**. Also on the wide deck: **SIREN**, **LIGHTS**, **FLIP** (for an upside-down picture), **FILL/FIT** and a big red **E-STOP**.

The **LEFT / RIGHT sliders** set how far the stick must travel before the wheels throw — higher is twitchier. They do *not* reduce steering power: the front rack is a full-throw actuator that the firmware only drives on its digital rails, so the steer command always stays above that threshold or the wheels would not move at all.

### 🎮 Xbox controller

Pair the pad to whichever device is showing the UI — the **tablet** (APK) or the **laptop** (browser at `http://<pc>:8788/`). The pad never talks to the ESP directly.

| Control | Does |
| --- | --- |
| **R2** | Go (analog) |
| **L2** | Brake, then reverse |
| **Left stick X** | Steer, scaled by the LEFT/RIGHT sliders |
| **B** / **Start** | E-STOP |
| **A** | Siren |
| **X** | Lights |
| **Y** | Toggle wide drive |

On Android the pad is read natively by the app shell, because a WebView's Gamepad API never sees a paired pad — without that the buttons only move focus around the screen.

---

Spiderman Trace-E robot: ESP32 firmware, local **speak_server** brain, desktop HQ UI, and Android mock/control shell.

## Quick start

```bash
pip install -r requirements.txt
```

### 1. Flash firmware (ESP32)

1. Open `firmware/trace_e_bot/trace_e_bot.ino` in Arduino IDE (or Arduino CLI).
2. Copy WiFi secrets locally (never commit the real file):

   ```bash
   copy firmware\trace_e_bot\wifi_config.h.example firmware\trace_e_bot\wifi_config.h
   ```

   Edit `wifi_config.h` with your SSID and password.
3. Select your ESP32 board + port, then **Upload**.
4. Serial Monitor shows the board IP when WiFi connects (or note it from your router).

### 2. API keys for Peanut voice (Ollie TTS)

Talk *through* Trace uses **Peanut Ana** as the primary voice (same character as peanut-robot), then dual cloud TTS:

| Order | Engine | Needs |
|-------|--------|--------|
| 1 | **edge-tts** `en-US-AnaNeural` (Peanut Ana) | free / no key — this is Peanut's real voice |
| 2 | **Groq** Orpheus TTS | `GROQ_API_KEY` (+ accept Orpheus terms once) |
| 3 | **Gemini** TTS | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| 4 | Spidey 101Soundboards (optional) | network |
| 5 | pyttsx3 | last-resort laptop voice only |

**Full click-path (Gemini terms + Groq Orpheus accept):** see [`desktop/TTS_KEYS.md`](desktop/TTS_KEYS.md).

Short version:

1. **Gemini:** [aistudio.google.com](https://aistudio.google.com/) → accept ToS → try TTS at [new_speech](https://aistudio.google.com/app/prompts/new_speech) → create key at [apikey](https://aistudio.google.com/apikey) → if API says paid/billing, use [billing setup](https://ai.google.dev/gemini-api/docs/billing) → set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`).
2. **Groq:** [console.groq.com/keys](https://console.groq.com/keys) → open [Orpheus playground](https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english) and **accept model terms** → set `GROQ_API_KEY`.

If Groq/Gemini TTS aren't ready yet, **Ana still speaks**. Cloud engines engage once keys + terms are valid.

```bash
copy .env.example .env
# set GROQ_API_KEY=... and GEMINI_API_KEY=...
```

Keys also load from `peanut-robot/.env`. Optional: `TRACE_E_TTS_ENGINE`, `GROQ_TTS_VOICE`, `GEMINI_TTS_VOICE`, `TRACE_E_ANA_VOICE`.
### 3. Run speak_server (local brain)

From the repo root:

```bash
python desktop/speak_server.py
```

Serves on **http://0.0.0.0:8788** by default (LAN-accessible):

| Path | Purpose |
|------|---------|
| `/` | Desktop `mock_ui.html` (WEB-Quarters) |
| `/android_mock.html` | Portrait phone mock + drive stick |
| `POST /api/speak` | Talk *through* Trace (Peanut TTS → ESP amp) |
| `POST /api/talk` | Talk *to* Trace (chat stub) |
| `GET /api/health` | Status + which TTS keys are set |

`POST /api/speak` JSON: `{ "text": "...", "engine": "peanut-auto", "esp": "http://192.168.1.108" }`  
Response includes `engine` used (e.g. `peanut-ana/edge-tts`, `groq/hannah`, `gemini/Kore`).

Other optional env:

- `TRACE_E_ESP_BASE` — ESP base URL (default `http://192.168.1.108`)
- `TRACE_E_SPEAK_PORT` — server port (default `8787`)
- `TRACE_E_CHIRPS` — default `off`

### 4. Open the UIs

With speak_server running:

- **Desktop HQ:** `http://192.168.1.105:8788/`
- **Android portrait mock:** `http://192.168.1.105:8788/android_mock.html`

Or open the HTML files directly:

- `desktop/mock_ui.html`
- `desktop/android_mock.html` — phone portrait chassis, **Flip cam**, **Fullscreen**, virtual **drive stick**, WASD

PyQt desktop deck (optional):

```bash
python desktop/trace_e_control.py
```

### 5. Connect the ESP

1. Put the ESP IP in the UI (**Set ESP**), e.g. `http://192.168.1.108`.
2. Drive with WASD or the stick — motor commands go to the ESP HTTP API.
3. **Talk through Trace** uses Peanut Ana / Groq / Gemini → WAV → ESP amp (laptop fallback if ESP play fails).
4. Camera stream (when available): ESP `:82/stream` — use **Flip cam** if the mount is upside-down.

Same WiFi for laptop/phone and the bot is required. Chirps stay **off** by default.

## Android app

See [README_ANDROID.md](README_ANDROID.md) and `android/TraceE/`.

## Layout

```
desktop/          speak_server, follow_person, mock_ui, android_mock, PyQt control
firmware/         ESP32 sketch + pins/motors (wifi_config.h gitignored)
android/          TraceE Android wrapper
ros2_ws/          Optional ROS 2 Jazzy Docker person-follow (same math as speak_server)
```

### Person follow (Z400 brain)

1. Run `python desktop/speak_server.py`
2. Open HQ UI → **Follow ON**
3. Stand in front of the cam — Trace drives fast when centered, turns slowly with differential
4. **WASD** cancels follow

Needs a live ESP cam on `:82/stream`. Optional ROS Docker: see `ros2_ws/README.md`.

### Occupancy SLAM (Z400 brain)

1. Run `python desktop/speak_server.py`
2. Open HQ UI → **Map ON**
3. Trace wanders, sketches a 12 m occupancy grid from PWM odom + cam optical flow + the front HC-SR04
4. Live map overlay sits on the cam; **Reset** clears the grid; **WASD** pauses explore (keeps mapping)
5. Say “map on” / “map off” to Talk

No lidar on this chassis — this is a room sketch, not Nav2. Docker: `docker compose --profile slam up` in `ros2_ws`.

## Secrets

Do **not** commit:

- `firmware/**/wifi_config.h`
- `.env`, API keys, keystores, `local.properties`

Use `.env.example` and `wifi_config.h.example` as templates only.

## Z400 takeover

Hand this repo to another PC (Z400): see **[HANDOFF.md](HANDOFF.md)** for clone URL, speak_server, ESP IP, flash FQBN, APK release links, brain IP, chirps-off, and TTS key copy notes (keys stay out of git).
