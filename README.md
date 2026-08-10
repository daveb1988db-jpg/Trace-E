# Trace-E Bot

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
| 2 | **Groq** Orpheus TTS | `GROQ_API_KEY` (+ accept Orpheus terms once in [Groq console](https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english)) |
| 3 | **Gemini** TTS | `GEMINI_API_KEY` or `GOOGLE_API_KEY` (Gemini TTS-capable key) |
| 4 | Spidey 101Soundboards (optional) | network |
| 5 | pyttsx3 | last-resort laptop voice only |

If Groq/Gemini TTS aren't ready yet, **Ana still speaks** (same voice peanut-robot used). Cloud engines engage automatically once keys + terms are valid.

Copy `.env.example` → `.env` in the Trace-E repo root **or** keep keys in `peanut-robot/.env` — `speak_server` loads both via dotenv (never commit real `.env`).

```bash
copy .env.example .env
# set GROQ_API_KEY=... and GEMINI_API_KEY=...
```

Optional overrides: `TRACE_E_TTS_ENGINE`, `GROQ_TTS_VOICE`, `GEMINI_TTS_VOICE`, `TRACE_E_ANA_VOICE`.

### 3. Run speak_server (local brain)

From the repo root:

```bash
python desktop/speak_server.py
```

Serves on **http://0.0.0.0:8787** by default:

| Path | Purpose |
|------|---------|
| `/` | Desktop `mock_ui.html` (WEB-Quarters) |
| `/android_mock.html` | Portrait phone mock + drive stick |
| `POST /api/speak` | Talk *through* Trace (Peanut TTS → ESP amp) |
| `POST /api/talk` | Talk *to* Trace (chat stub) |
| `GET /api/health` | Status + which TTS keys are set |

`POST /api/speak` JSON: `{ "text": "...", "engine": "peanut-auto", "esp": "http://192.168.1.104" }`  
Response includes `engine` used (e.g. `peanut-ana/edge-tts`, `groq/hannah`, `gemini/Kore`).

Other optional env:

- `TRACE_E_ESP_BASE` — ESP base URL (default `http://192.168.1.104`)
- `TRACE_E_SPEAK_PORT` — server port (default `8787`)
- `TRACE_E_CHIRPS` — default `off`

### 4. Open the UIs

With speak_server running:

- **Desktop HQ:** [http://127.0.0.1:8787/](http://127.0.0.1:8787/)
- **Android portrait mock:** [http://127.0.0.1:8787/android_mock.html](http://127.0.0.1:8787/android_mock.html)

Or open the HTML files directly:

- `desktop/mock_ui.html`
- `desktop/android_mock.html` — phone portrait chassis, **Flip cam**, **Fullscreen**, virtual **drive stick**, WASD

PyQt desktop deck (optional):

```bash
python desktop/trace_e_control.py
```

### 5. Connect the ESP

1. Put the ESP IP in the UI (**Set ESP**), e.g. `http://192.168.1.104`.
2. Drive with WASD or the stick — motor commands go to the ESP HTTP API.
3. **Talk through Trace** uses Peanut Ana / Groq / Gemini → WAV → ESP amp (laptop fallback if ESP play fails).
4. Camera stream (when available): ESP `:82/stream` — use **Flip cam** if the mount is upside-down.

Same WiFi for laptop/phone and the bot is required. Chirps stay **off** by default.

## Android app

See [README_ANDROID.md](README_ANDROID.md) and `android/TraceE/`.

## Layout

```
desktop/          speak_server, mock_ui, android_mock, PyQt control
firmware/         ESP32 sketch + pins/motors (wifi_config.h gitignored)
android/          TraceE Android wrapper
```

## Secrets

Do **not** commit:

- `firmware/**/wifi_config.h`
- `.env`, API keys, keystores, `local.properties`

Use `.env.example` and `wifi_config.h.example` as templates only.
