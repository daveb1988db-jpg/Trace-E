# Trace-E Android app

Kid-friendly **Trace-E** control centre for Android phones. WebView loads the WEB-Quarters UI from app assets; MJPEG is proxied in-app; drive goes **direct** to the ESP on `:8765`.

## Install (sideload)

1. Download **`Trace-E-v0.1.2.apk`** from the [GitHub Releases](https://github.com/daveb1988db-jpg/Trace-E/releases) page.
2. On the phone: allow **Install unknown apps** for your browser/Files app (Settings → Security / Apps).
3. Open the APK and install.
4. Join the **same Wi‑Fi** as the robot (not guest / VPN).
5. Open Trace-E → **Set ESP** to your board IP (default `http://192.168.1.104`).
6. **Voice (Talk to / through Trace):** on the laptop run `python desktop/speak_server.py`, note the laptop’s Wi‑Fi IP (e.g. `192.168.1.102`), then in the app tap **Set Brain** to `http://192.168.1.102:8787`. Never use `127.0.0.1` — that is only the laptop itself, not reachable from the tablet.

Debug-signed release APK — fine for family/friends sideload, not Play Store.

## How it connects

```
Phone (Trace-E app)
  ├─ WebView → assets/www/index.html
  ├─ Drive stick / WASD  → ESP :8765/api/drive   (direct, low lag)
  ├─ Live cam MJPEG      → in-app HttpURLConnection proxy → ESP :82/stream
  └─ Talk to / through   → PC speak_server :8787 (same LAN)
                           ESP alone cannot do TTS
```

| Service | Default | Role |
|--------|---------|------|
| ESP32 Trace-E | `http://192.168.1.104` | Drive `:8765`, status |
| Cam MJPEG | `http://192.168.1.104:82/stream` | Live view |
| speak_server | `http://192.168.1.102:8787` (editable) | Talk to Trace / Talk through Trace |

Stick tuning (kid-friendly): deadzone `0.28`, steer expo `2.2`, roll steer gain `0.38`, spin gain `0.50`.

Portrait by default; landscape (or Fullscreen) goes immersive. Chirps stay **off**.

## Build from source

Prerequisites: JDK 17+ (Android Studio JBR works), Android SDK.

```bash
cd android/TraceE
# local.properties with sdk.dir=... is created locally (gitignored)
./gradlew assembleRelease
# APK: app/build/outputs/apk/release/app-release.apk
```

```bash
adb install -r app/build/outputs/apk/release/app-release.apk
```

## Project layout

```
android/TraceE/
  gradlew / gradlew.bat
  app/src/main/
    AndroidManifest.xml          # cleartext HTTP allowed
    java/com/tracee/bot/
      MainActivity.kt
      MjpegProxyServer.kt
    assets/www/index.html        # Trace-E HQ UI
    res/xml/network_security_config.xml
```

## Personal-use art note

Spidey Friends theming is for **personal / family** Trace-E HQ use. Swap branding before any commercial / Play Store listing.
