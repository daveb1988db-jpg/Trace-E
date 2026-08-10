# Trace-E Android app

Kid-friendly **Trace-E** control centre for Android phones. Wraps the WEB-Quarters control UI in a WebView and proxies MJPEG from the ESP so the live cam stays smooth.

**Status:** local scaffold only — **not** pushed to GitHub / Play Store yet. Approve the mock UI first.

## How it connects to the robot

```
Phone (Trace-E app)
  ├─ WebView → file:///android_asset/www/index.html  (WEB-Quarters UI)
  ├─ Drive / gestures / speak APIs
  │     → PC brain: http://<PC-LAN-IP>:8787  (desktop/speak_server.py)
  │     → or direct ESP HTTP when brain is offline (drive only)
  └─ Live cam MJPEG
        → App-local HttpURLConnection proxy (:internal)
        → ESP stream http://192.168.1.104:82/stream  (typical Trace / peanut cam)
```

Typical home LAN:

| Service | Default | Role |
|--------|---------|------|
| ESP32 Trace-E | `http://192.168.1.104` | Drive, amp, status |
| Cam MJPEG | `http://192.168.1.104:82/stream` | Live view |
| speak_server | `http://<PC>:8787` | Talk to Trace / Talk through Trace / drive proxy |

Phone and robot must be on the **same Wi‑Fi**. Set ESP IP in the app (Set ESP). Flip cam cycles OFF / ON (v-flip) / 180°.

## Architecture choice: WebView + stream proxy

Prefer **Kotlin WebView** loading a local copy of the HQ page (not Compose-from-scratch) so:

- UI matches desktop WEB-Quarters immediately
- MJPEG can be fetched with `HttpURLConnection` in a tiny in-app proxy and fed to an `<img>` / blob URL (avoids WebView stalling on multipart streams)
- Zero-lag drive stays on ESP HTTP from the page (or brain `/api/esp/drive`)

Optional later: load `http://ESP:82/stream` carefully inside WebView; proxy is more reliable on Android.

## Project layout

```
android/TraceE/
  settings.gradle.kts
  build.gradle.kts
  gradle.properties
  app/
    build.gradle.kts
    src/main/
      AndroidManifest.xml
      java/com/tracee/bot/
        MainActivity.kt
        MjpegProxyServer.kt
      assets/www/
        index.html          ← ship a mobile HQ page (from desktop mock / mock_ui)
        (copy assets/ images as needed)
      res/values/strings.xml
      res/xml/network_security_config.xml
```

## Build an installable APK (local)

Prerequisites: Android Studio Ladybug+ (or JDK 17 + Android SDK), phone/emulator API 26+.

```bash
cd android/TraceE
# With Android Studio: Open folder → Build → Build Bundle(s) / APK(s) → Build APK(s)
# Or CLI once wrapper exists:
./gradlew assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

Sideload:

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Release signing (when ready): create a keystore, set `signingConfigs` in `app/build.gradle.kts`, then `assembleRelease`.

## GitHub release plan (ONLY after user says yes)

Do **not** push until the mock is approved.

1. Confirm GitHub auth: `gh auth status` (expected account related to **daveb1988db-jpg** from peanut work).
2. Create private or public repo under that account named **`Trace-E`** (or `Trace-E-Bot` if preferred).
3. Push this workspace (or `android/TraceE` + desktop HQ) — no secrets, no `_secrets`, no local IPs hard-locked if avoidable.
4. Tag `v0.1.0-android-mock` or `v0.1.0` after first signed APK.
5. `gh release create v0.1.0 --title "Trace-E Android" --notes "..." ./app-release.apk`
6. README badge + install steps for family/friends (personal / Spidey Friends art — keep personal-use notice).

Until then: everything stays on disk under `C:\Users\Bartl\Documents\Trace-E-Bot`.

## Mock UI (approve first)

Open in a browser:

- `desktop/android_mock.html` — portrait phone frame, kid-friendly WEB-Quarters

Desktop full HQ (reference):

- `desktop/mock_ui.html`

## Personal-use art note

Spidey and His Amazing Friends imagery under `desktop/assets/` is for **personal / family** Trace-E HQ use. Do not publish commercial builds with that art without rights clearance; swap to original Trace-E branding for any public store listing.
