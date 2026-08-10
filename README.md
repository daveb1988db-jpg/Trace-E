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

### 2. Run speak_server (local brain)

From the repo root:

```bash
python desktop/speak_server.py
```

Serves on **http://0.0.0.0:8787** by default:

| Path | Purpose |
|------|---------|
| `/` | Desktop `mock_ui.html` (WEB-Quarters) |
| `/android_mock.html` | Portrait phone mock + drive stick |
| `POST /api/speak` | Talk *through* Trace (TTS → ESP amp) |
| `POST /api/talk` | Talk *to* Trace (chat stub) |

Optional env:

- `TRACE_E_ESP_BASE` — ESP base URL (default `http://192.168.1.104`)
- `TRACE_E_SPEAK_PORT` — server port (default `8787`)

### 3. Open the UIs

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

### 4. Connect the ESP

1. Put the ESP IP in the UI (**Set ESP**), e.g. `http://192.168.1.104`.
2. Drive with WASD or the stick — motor commands go to the ESP HTTP API.
3. **Talk through Trace** uses speak_server → ESP amp (laptop TTS fallback if ESP play fails).
4. Camera stream (when available): ESP `:82/stream` — use **Flip cam** if the mount is upside-down.

Same WiFi for laptop/phone and the bot is required.

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

Use `wifi_config.h.example` as the template only.
