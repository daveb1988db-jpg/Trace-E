# Accept Gemini + Groq TTS (API keys)

Trace-E `speak_server` reads keys from `Trace-E-Bot/.env` and/or `peanut-robot/.env`.

| Env var | Used for |
|---------|----------|
| `GROQ_API_KEY` | Groq Orpheus / PlayAI TTS |
| `GEMINI_API_KEY` | Gemini native TTS (preferred name) |
| `GOOGLE_API_KEY` | Same as Gemini (alias if `GEMINI_API_KEY` empty) |

Docs: [Gemini speech generation](https://ai.google.dev/gemini-api/docs/speech-generation) · [Groq Orpheus TTS](https://console.groq.com/docs/text-to-speech/orpheus)

---

## A) Accept / enable **Gemini TTS**

1. Open [Google AI Studio](https://aistudio.google.com/) and sign in with your Google account. Accept the Generative AI terms if prompted (first visit).
2. Open the TTS playground and generate a short clip (this unlocks / validates TTS for your account):
   - Direct: [Speech generation](https://aistudio.google.com/app/prompts/new_speech)
   - Or: AI Studio → **Playground** / **Generate media** → **Gemini TTS** / speech
3. Create or copy an API key: [AI Studio API keys](https://aistudio.google.com/apikey) → **Create API key** → copy the key.
4. If TTS API calls return billing / paid-tier errors (common for Pro TTS or after free limits):
   - [Set up billing in AI Studio](https://aistudio.google.com/) (look for **Set up billing** on the API keys / Projects page)
   - Or follow [Gemini API billing](https://ai.google.dev/gemini-api/docs/billing)
5. Put the key in `.env` (repo root or peanut-robot):

   ```env
   GEMINI_API_KEY=your_key_here
   # optional alias:
   # GOOGLE_API_KEY=your_key_here
   ```

6. Restart `python desktop/speak_server.py`. Check `GET http://127.0.0.1:8787/api/health` → `tts.keys.gemini` should be `true`.
7. Optional smoke test in UI: Talk through Trace with `engine: "gemini"`.

**Notes**

- Trace-E models tried: `gemini-2.5-flash-preview-tts`, `gemini-2.5-pro-preview-tts`, `gemini-3.1-flash-tts-preview`.
- Flash TTS often works on free / limited quota; Pro TTS usually needs billing.
- Voice default: `GEMINI_TTS_VOICE=Kore` (override in `.env`).

---

## B) Accept **Groq Orpheus** model terms

1. Create a free key: [Groq Console → API Keys](https://console.groq.com/keys).
2. **Accept Orpheus terms** (required once per account or API calls fail):
   - English: [Orpheus playground](https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english)
   - Open that page while logged in → accept the model / terms dialog if shown → optionally run a short playground generate.
3. Orpheus docs: [console.groq.com/docs/text-to-speech/orpheus](https://console.groq.com/docs/text-to-speech/orpheus)
4. Put the key in `.env`:

   ```env
   GROQ_API_KEY=your_key_here
   # GROQ_TTS_MODEL=canopylabs/orpheus-v1-english
   # GROQ_TTS_VOICE=hannah
   ```

5. Restart speak_server. Health check → `tts.keys.groq` should be `true`.

**Notes**

- Orpheus `response_format` must be `wav` (speak_server already does this).
- If Orpheus terms aren’t accepted yet, speak_server also tries PlayAI voices as a fallback.

---

## Quick `.env` template

```env
GROQ_API_KEY=
GEMINI_API_KEY=
# GOOGLE_API_KEY=
# TRACE_E_TTS_ENGINE=peanut-auto
```

Copy from `.env.example` if needed. Never commit real `.env` files.

---

## Laptop no sound (Windows checklist)

If Talk-through falls back to laptop and you hear nothing:

1. **Settings → System → Sound** → Output = **Speakers (Realtek(R) Audio)** (not Oculus / Bluetooth).
2. Volume slider up; click the speaker icon and ensure it is not muted.
3. **Volume mixer** — unmute System sounds / Python / Cursor / Chrome.
4. Device → **Additional device properties → Advanced** → uncheck **Allow applications to take exclusive control of this device** → Apply.
5. Unplug any half-inserted headphone jack; disable **Headphones (Oculus Virtual Audio Device)** if present.
6. Optional admin: stop **Oculus VR Runtime Service** (`OVRService`) in `services.msc`.
7. Re-test: play `C:\Windows\Media\Windows Notify.wav` (double-click) or run `python desktop/_audio_fix.py`.
