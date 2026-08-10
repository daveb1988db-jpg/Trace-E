# Z400 takeover — Trace-E

Clone and pick up where this machine left off. **Do not commit API keys or WiFi passwords.**

## Clone

```bash
git clone https://github.com/daveb1988db-jpg/Trace-E.git
cd Trace-E
```

HTTPS clone URL: `https://github.com/daveb1988db-jpg/Trace-E.git`

## Network defaults (lab LAN)

| Role | Default |
|------|---------|
| ESP Trace-E | `http://192.168.1.104` (drive `:8765`, cam `:82/stream`) |
| Brain (speak_server) | laptop WiFi IP, port **8787** — e.g. `http://192.168.1.102:8787` |
| Chirps | **off** (`TRACE_E_CHIRPS=off`) |

Phone/tablet must use the brain LAN IP, never `127.0.0.1`.

## Secrets (copy separately — not in git)

1. Copy `.env.example` → `.env` and fill keys, **or** copy Peanut Ana / TTS keys from `peanut-robot/.env`:
   - `GROQ_API_KEY`
   - `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)
2. Firmware WiFi:
   ```bash
   copy firmware\trace_e_bot\wifi_config.h.example firmware\trace_e_bot\wifi_config.h
   ```
   Edit SSID/password locally. `wifi_config.h` is gitignored.

Key setup walkthrough: [`desktop/TTS_KEYS.md`](desktop/TTS_KEYS.md).

## First steps on Z400

1. `pip install -r requirements.txt`
2. Ensure `.env` has TTS keys (from peanut-robot / local copy).
3. Run brain:
   ```bash
   python desktop/speak_server.py
   ```
   HQ UI: http://127.0.0.1:8787/ — health: http://127.0.0.1:8787/api/health
4. Flash ESP if needed (Arduino CLI / IDE), FQBN:
   ```
   esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,UploadSpeed=115200
   ```
   Sketch: `firmware/trace_e_bot/trace_e_bot.ino` (ESP32-S3 Freenove N16R8).
5. Confirm ESP at **192.168.1.104** (or update UI / `TRACE_E_ESP_BASE`).
6. Android: install APK from Releases (sources are v0.1.2):
   - Latest: https://github.com/daveb1988db-jpg/Trace-E/releases/tag/v0.1.2
   - APK: https://github.com/daveb1988db-jpg/Trace-E/releases/download/v0.1.2/Trace-E-v0.1.2.apk
   - Also: [v0.1.1](https://github.com/daveb1988db-jpg/Trace-E/releases/tag/v0.1.1), [v0.1.0](https://github.com/daveb1988db-jpg/Trace-E/releases/tag/v0.1.0)
7. In app: **Set ESP** → `http://192.168.1.104`; **Set Brain** → `http://<Z400-LAN-IP>:8787`.

## What landed in this handoff commit

- `speak_server` amp / Peanut Ana TTS path
- Desktop `mock_ui` / `android_mock` lag fixes
- Android app sources (v0.1.2)
- Firmware amp path updates
- README + this handoff

APKs and giant `build/` trees stay out of git (Releases + local builds only).
