/*
 * Trace-E Bot — camera + differential drive
 *
 * Board: ESP32-S3 Freenove (OV2640 FPC) + L9110S chassis
 * Identity: model=trace-e (NOT Peanut)
 *
 * Endpoints:
 *   GET  http://<ip>/api/status          (:80)
 *   GET  http://<ip>:8765/api/status
 *   GET/POST http://<ip>:8765/api/drive?left=&right=   (signed −100..+100)
 *   MJPEG http://<ip>:82/stream          (dedicated — keeps :80/:8765 alive)
 *   GET  http://<ip>/capture             JPEG snapshot (also :8765/capture)
 *   GET  http://<ip>/api/chase?on=0|1    on-ESP bright-orange chase (no drive lag)
 *   GET  http://<ip>:8765/api/reboot     soft restart
 *
 * WASD matrices (client-side; firmware accepts raw L/R):
 *   W 100/100  S -100/-100
 *   A  +38/-38 (spin LEFT slow)  D  -38/+38 (spin RIGHT slow)
 *   W+A 100/10  W+D 10/100  (strong curve — inner drop)
 *   S+A -100/-10  S+D -10/-100  stop 0/0
 *   Note: A/D swapped vs classic tank docs to match this chassis.
 *
 * Flash FQBN:
 *   esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,UploadSpeed=115200
 */

#include <WiFi.h>
#include <WebServer.h>
#include <WiFiClient.h>
#include <WiFiUdp.h>
#include <esp_camera.h>
#include "pins.h"
#include "motors.h"
#include "amp.h"
#include "chase.h"

#if __has_include("wifi_config.h")
#include "wifi_config.h"
#define HAS_WIFI_CONFIG 1
#else
#define HAS_WIFI_CONFIG 0
#endif

#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif

static const char *MODEL_ID = "trace-e";
static const char *MIC_FW_TAG = "trace-e-mic-wav-1";
static const char *FW_NAME = "Trace-E Bot";

// Safety net only. The UI resends the held drive command every ~300ms, so this
// must stay several repeats wide or a single dropped packet stutters the motors.
// Do not shorten below ~1s without shortening the UI repeat interval to match.
static const unsigned long DRIVE_FAILSAFE_MS = 1200;
// Amazon FPC OV2640 (POFET B0D6Y77T9N fisheye 160° / UXGA sensor on Freenove FPC).
// VGA + Q10 restores the pre-regression resolution; the brain forwards it untouched.
// This also sets the ceiling for /api/camera_quality: the driver allocates buffers for
// frame_size at init, so a runtime request larger than this is silently clamped down.
static const int CAM_JPEG_Q = 10;
static const framesize_t CAM_FRAMESIZE = FRAMESIZE_VGA;  // 640x480
// 10 MHz halved the pixel clock and roughly doubled frame latency. 16 MHz keeps the
// margin these cheap OV2640 boards need while restoring most of the frame rate.
static const int CAM_XCLK_HZ = 16000000;

static WebServer server(80);
static WebServer driveSrv(8765);
static WiFiServer streamServer(82);
// Control link: UDP is fire-and-forget, so a held key never waits on a TCP
// handshake behind the MJPEG stream. Video stays on TCP, control on UDP.
static const uint16_t DRIVE_UDP_PORT = 8767;
static WiFiUDP driveUdp;
static volatile unsigned long g_udpPackets = 0;

static bool g_wifiOk = false;
static bool g_camOk = false;
static String g_ip = "";
static int g_left = 0;
static int g_right = 0;
static unsigned long g_lastCmdMs = 0;
static volatile bool g_camStreaming = false;
static bool g_headlights = false;  // pretend headlights on GPIO38 — off until UI says on
static int g_headlightBrightness = 0;

static void corsHeaders(WebServer &s) {
  s.sendHeader("Access-Control-Allow-Origin", "*");
  s.sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  s.sendHeader("Access-Control-Allow-Headers", "*");
  s.sendHeader("Cache-Control", "no-store");
}

/** Last HC-SR04 sample (cached so status polls don't hammer pulseIn). */
static float g_usCm = -1.0f;
static unsigned long g_usMs = 0;

/** HC-SR04 range in cm; -1 if no echo / wiring missing. Short timeout (~12ms ≈ 2m). */
static float readUltrasonicCm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  // 12000us ≈ 2m round-trip; keeps /api/status + drive path snappy
  unsigned long us = pulseIn(PIN_ECHO, HIGH, 12000UL);
  if (us == 0) return -1.0f;
  float cm = us / 58.0f;
  if (cm < 2.0f || cm > 400.0f) return -1.0f;
  return cm;
}

static float ultrasonicCachedCm() {
  unsigned long now = millis();
  if (now - g_usMs >= 60UL || g_usMs == 0) {
    float v = readUltrasonicCm();
    if (v > 0) {
      g_usCm = v;
      g_usMs = now;
    } else if (now - g_usMs > 250UL) {
      g_usCm = -1.0f;
      g_usMs = now;
    }
  }
  return g_usCm;
}

/** MAX4466 peak deviation from mid-scale (0..1-ish). */
static float readMicLevel() {
  if (PIN_MIC < 0) return 0.0f;
  const int mid = 2048;
  int peak = 0;
  for (int i = 0; i < 48; i++) {
    int v = analogRead(PIN_MIC);
    int d = v - mid;
    if (d < 0) d = -d;
    if (d > peak) peak = d;
  }
  float lvl = peak / 1800.0f;
  if (lvl < 0.0f) lvl = 0.0f;
  if (lvl > 1.0f) lvl = 1.0f;
  return lvl;
}

/** Record Trace's onboard MAX4466 to a mono WAV for brain STT (cover-to-talk). */
static void handleMicWav(WebServer &s) {
  corsHeaders(s);
  if (PIN_MIC < 0) {
    s.send(503, "application/json", "{\"ok\":false,\"error\":\"no mic pin\"}");
    return;
  }
  if (chaseEnabled() || g_left != 0 || g_right != 0 ||
      (g_lastCmdMs != 0 && (millis() - g_lastCmdMs) < 800UL)) {
    s.send(503, "application/json", "{\"ok\":false,\"error\":\"busy driving\"}");
    return;
  }
  // Keep short — ESP RAM + Whisper latency for a kids toy
  float secs = 3.0f;
  if (s.hasArg("seconds")) {
    secs = s.arg("seconds").toFloat();
  }
  if (secs < 1.0f) secs = 1.0f;
  if (secs > 4.0f) secs = 4.0f;
  const int sr = 8000;
  const int n = (int)(secs * sr);
  size_t bytes = (size_t)n * 2u;
  int16_t *pcm = (int16_t *)ps_malloc(bytes);
  if (!pcm) {
    pcm = (int16_t *)malloc(bytes);
  }
  if (!pcm) {
    s.send(500, "application/json", "{\"ok\":false,\"error\":\"oom\"}");
    return;
  }

  // Coast motors while recording so drive PWM noise is quieter
  motorsCoast();
  g_left = 0;
  g_right = 0;

  analogReadResolution(12);
  const uint32_t periodUs = 1000000UL / (uint32_t)sr;
  const int mid = 2048;
  for (int i = 0; i < n; i++) {
    uint32_t t0 = micros();
    int v = analogRead(PIN_MIC);
    int centered = (v - mid) * 16;  // boost quiet MAX4466 into int16 range
    if (centered > 32767) centered = 32767;
    if (centered < -32768) centered = -32768;
    pcm[i] = (int16_t)centered;
    while ((micros() - t0) < periodUs) {
      // spin
    }
  }

  // Build WAV in one buffer (header + pcm)
  const size_t wavBytes = 44u + bytes;
  uint8_t *wav = (uint8_t *)ps_malloc(wavBytes);
  if (!wav) {
    wav = (uint8_t *)malloc(wavBytes);
  }
  if (!wav) {
    free(pcm);
    s.send(500, "application/json", "{\"ok\":false,\"error\":\"oom wav\"}");
    return;
  }
  auto w32 = [&](size_t off, uint32_t v) {
    wav[off] = (uint8_t)(v & 0xff);
    wav[off + 1] = (uint8_t)((v >> 8) & 0xff);
    wav[off + 2] = (uint8_t)((v >> 16) & 0xff);
    wav[off + 3] = (uint8_t)((v >> 24) & 0xff);
  };
  auto w16 = [&](size_t off, uint16_t v) {
    wav[off] = (uint8_t)(v & 0xff);
    wav[off + 1] = (uint8_t)((v >> 8) & 0xff);
  };
  memcpy(wav, "RIFF", 4);
  w32(4, 36u + (uint32_t)bytes);
  memcpy(wav + 8, "WAVEfmt ", 8);
  w32(16, 16);
  w16(20, 1);   // PCM
  w16(22, 1);   // mono
  w32(24, (uint32_t)sr);
  w32(28, (uint32_t)sr * 2u);
  w16(32, 2);
  w16(34, 16);
  memcpy(wav + 36, "data", 4);
  w32(40, (uint32_t)bytes);
  memcpy(wav + 44, pcm, bytes);
  free(pcm);

  s.sendHeader("Content-Disposition", "inline; filename=\"trace_mic.wav\"");
  s.sendHeader("Cache-Control", "no-store");
  s.setContentLength(wavBytes);
  s.send(200, "audio/wav", "");
  WiFiClient cl = s.client();
  size_t off = 0;
  while (off < wavBytes && cl.connected()) {
    size_t chunk = wavBytes - off;
    if (chunk > 1024) chunk = 1024;
    size_t n = cl.write(wav + off, chunk);
    if (n == 0) break;
    off += n;
  }
  free(wav);
}

static String statusJson() {
  float usCm = ultrasonicCachedCm();
  float micLvl = (g_left || g_right) ? 0.0f : readMicLevel();
  String ip = g_wifiOk ? WiFi.localIP().toString() : String("");
  String body = "{";
  body += "\"ok\":true,";
  body += "\"model\":\"";
  body += MODEL_ID;
  body += "\",";
  body += "\"name\":\"";
  body += FW_NAME;
  body += "\",";
  body += "\"mic_fw\":\"";
  body += MIC_FW_TAG;
  body += "\",";
  body += "\"wifi\":";
  body += g_wifiOk ? "true" : "false";
  body += ",";
  body += "\"ip\":\"";
  body += ip;
  body += "\",";
  body += "\"uptime_ms\":";
  body += String(millis());
  body += ",";
  body += "\"cam\":";
  body += g_camOk ? "true" : "false";
  body += ",";
  body += "\"cam_streaming\":";
  body += g_camStreaming ? "true" : "false";
  body += ",";
  body += "\"chase\":";
  body += chaseEnabled() ? "true" : "false";
  body += ",";
  {
    bool found = false;
    int cx = 0, pixels = 0;
    float chaseUs = -1.0f;
    chaseStatus(&found, &cx, &pixels, &chaseUs);
    body += "\"chase_found\":";
    body += found ? "true" : "false";
    body += ",";
    body += "\"chase_cx\":";
    body += String(cx);
    body += ",";
    body += "\"chase_pixels\":";
    body += String(pixels);
    body += ",";
    if (chaseEnabled()) {
      body += "\"left\":";
      body += String(chaseCmdLeft());
      body += ",";
      body += "\"right\":";
      body += String(chaseCmdRight());
      body += ",";
    } else {
      body += "\"left\":";
      body += String(g_left);
      body += ",";
      body += "\"right\":";
      body += String(g_right);
      body += ",";
    }
  }
  body += "\"stream\":\"http://";
  body += ip;
  body += ":82/stream\",";
  body += "\"capture\":\"http://";
  body += ip;
  body += "/capture\",";
  body += "\"drive\":\"http://";
  body += ip;
  body += ":8765/api/drive\",";
  body += "\"volume\":";
  body += String(ampVolumeGet());
  body += ",";
  body += "\"amp\":true,";
  body += "\"us_trig\":";
  body += String(PIN_TRIG);
  body += ",";
  body += "\"us_echo\":";
  body += String(PIN_ECHO);
  body += ",";
  if (usCm > 0) {
    body += "\"ultrasonic_cm\":";
    body += String(usCm, 1);
    body += ",";
    body += "\"distance_cm\":";
    body += String(usCm, 1);
    body += ",";
  } else {
    body += "\"ultrasonic_cm\":null,";
    body += "\"distance_cm\":null,";
  }
  body += "\"mic_level\":";
  body += String(micLvl, 3);
  body += ",";
  body += "\"mic_peak\":";
  body += String(micLvl, 3);
  body += ",";
  {
    int tl = 100, tr = 100;
    motorsTrimGet(&tl, &tr);
    body += "\"trim_left\":";
    body += String(tl);
    body += ",";
    body += "\"trim_right\":";
    body += String(tr);
    body += ",";
    body += "\"bumper_cm\":10,";
    body += "\"bumper_blocked\":";
    body += motorsBumperBlocked() ? "true" : "false";
    body += ",";
  }
  body += "\"drive_udp_port\":";
  body += String(DRIVE_UDP_PORT);
  body += ",";
  body += "\"udp_packets\":";
  body += String(g_udpPackets);
  body += ",";
  body += "\"headlights\":";
  body += g_headlights ? "true" : "false";
  body += ",";
  body += "\"headlight_brightness\":";
  body += String(g_headlightBrightness);
  body += ",";
  body += "\"sensors\":[\"camera\",\"ultrasonic\",\"mic\"]";
  body += "}";
  return body;
}

static void handleStatus(WebServer &s) {
  corsHeaders(s);
  s.send(200, "application/json", statusJson());
}

static void handleOptions(WebServer &s) {
  corsHeaders(s);
  s.send(204);
}

static void handleCapture(WebServer &s) {
  corsHeaders(s);
  if (!g_camOk && !chaseEnabled()) {
    s.send(503, "text/plain", "camera not ready");
    return;
  }
  if (chaseEnabled()) {
    uint8_t *jpg = nullptr;
    size_t len = 0;
    if (!chaseCloneJpeg(&jpg, &len)) {
      s.send(503, "text/plain", "chase preview not ready");
      return;
    }
    s.sendHeader("Content-Disposition", "inline; filename=trace.jpg");
    s.setContentLength(len);
    s.send(200, "image/jpeg", "");
    WiFiClient client = s.client();
    client.write(jpg, len);
    free(jpg);
    return;
  }
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb || !fb->buf || fb->len < 100) {
    if (fb) esp_camera_fb_return(fb);
    s.send(503, "text/plain", "capture failed");
    return;
  }
  s.sendHeader("Content-Disposition", "inline; filename=trace.jpg");
  s.setContentLength(fb->len);
  s.send(200, "image/jpeg", "");
  WiFiClient client = s.client();
  client.write(fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

static void handleReboot(WebServer &s) {
  corsHeaders(s);
  s.send(200, "application/json", "{\"ok\":true,\"rebooting\":true}");
  delay(200);
  ESP.restart();
}

static void handleRoot() {
  String html = "<!doctype html><html><head><meta charset=utf-8>";
  html += "<title>Trace-E Bot</title></head><body>";
  html += "<h1>Trace-E Bot</h1>";
  html += "<p>model=<code>trace-e</code></p><ul>";
  html += "<li><a href=/api/status>/api/status</a></li>";
  html += "<li>Drive: <code>:8765/api/drive?left=&amp;right=</code></li>";
  html += "<li>Cam: <a href=http://";
  html += g_ip;
  html += ":82/stream>:82/stream</a></li>";
  html += "</ul></body></html>";
  server.send(200, "text/html", html);
}

// Rack steer shaping tunables — live-editable via /api/steer (see applyDriveCmd).
static unsigned long STEER_KICK_MS = 180;
static unsigned long STEER_LOCK_MS = 60000;
static int STEER_HOLD_PCT = 100;
// Per-side snap-back: the rack is not symmetric (A drives GPIO42, D GPIO46),
// so left and right releases need their own burst strength/duration.
static unsigned long STEER_CENTER_MS_L = 140;
static unsigned long STEER_CENTER_MS_R = 140;
static int STEER_CENTER_PCT_L = 25;
static int STEER_CENTER_PCT_R = 25;
static bool STEER_SNAPBACK = true;

static int shapedSteer();

static void driveFailsafeTick() {
  if (chaseEnabled()) return;  // local chase owns motors (still uses driveLR bumper)
  // Live bumper while holding W: only rear drive (API right) is forward.
  if (g_right > 8) {
    float cm = ultrasonicCachedCm();
    // Match driveLR: only stop on a valid close echo (not missing/glitch)
    if (cm > 0.0f && cm < 10.0f) {
      setMotor(MOTOR_LEFT, shapedSteer());  // keep rack (shaped, never stalled at 100)
      setMotor(MOTOR_RIGHT, 0);
      g_right = 0;
      g_lastCmdMs = millis();  // keep failsafe armed while still steering
      return;
    }
  }
  if (g_lastCmdMs == 0) return;
  if ((millis() - g_lastCmdMs) <= DRIVE_FAILSAFE_MS) return;
  if (g_left != 0 || g_right != 0) {
    motorsCoast();
    g_left = 0;
    g_right = 0;
    Serial.println("drive failsafe → coast");
  }
  g_lastCmdMs = 0;
}

static void handleChase(WebServer &s) {
  corsHeaders(s);
  bool want = chaseEnabled();
  if (s.hasArg("on")) {
    want = s.arg("on").toInt() != 0;
  } else if (s.hasArg("enable")) {
    want = s.arg("enable").toInt() != 0;
  } else if (s.hasArg("chase")) {
    want = s.arg("chase").toInt() != 0;
  }
  bool ok = chaseSetEnabled(want);
  // Camera stays in JPEG mode now — g_camOk unchanged unless enable failed hard.
  if (ok && want) {
    g_left = 0;
    g_right = 0;
    g_lastCmdMs = 0;
  }
  char buf[160];
  snprintf(buf, sizeof(buf),
           "{\"ok\":%s,\"chase\":%s,\"cam\":%s,\"model\":\"trace-e\"}",
           ok ? "true" : "false",
           chaseEnabled() ? "true" : "false",
           g_camOk ? "true" : "false");
  s.send(ok ? 200 : 503, "application/json", buf);
}

static void setHeadlightBrightness(int percent) {
  percent = constrain(percent, 0, 100);
  g_headlightBrightness = percent;
  g_headlights = percent > 0;
  uint8_t duty = (uint8_t)((percent * 255 + 50) / 100);
  if (HEADLIGHTS_ACTIVE_LOW) duty = 255 - duty;  // pin sinks from 3.3V rail
  ledcWrite(PIN_HEADLIGHTS, duty);
}

static void setHeadlights(bool on) {
  setHeadlightBrightness(on ? 100 : 0);
}

static void handleHeadlights(WebServer &s) {
  corsHeaders(s);
  int brightness = g_headlightBrightness;
  if (s.hasArg("brightness")) {
    brightness = s.arg("brightness").toInt();
  } else if (s.hasArg("level")) {
    brightness = s.arg("level").toInt();
  } else if (s.hasArg("on")) {
    brightness = s.arg("on").toInt() != 0 ? 100 : 0;
  } else if (s.hasArg("enable") || s.hasArg("lights") || s.hasArg("headlights")) {
    String k = s.hasArg("enable") ? "enable" : (s.hasArg("lights") ? "lights" : "headlights");
    brightness = s.arg(k).toInt() != 0 ? 100 : 0;
  }
  setHeadlightBrightness(brightness);
  char buf[128];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"headlights\":%s,\"brightness\":%d,\"model\":\"trace-e\"}",
           g_headlights ? "true" : "false", g_headlightBrightness);
  s.send(200, "application/json", buf);
}

static void handleBumper(WebServer &s) {
  corsHeaders(s);
  bool want = motorsBumperEnabled();
  if (s.hasArg("on")) {
    want = s.arg("on").toInt() != 0;
  } else if (s.hasArg("enable") || s.hasArg("bumper")) {
    String k = s.hasArg("enable") ? "enable" : "bumper";
    want = s.arg(k).toInt() != 0;
  }
  motorsSetBumperEnabled(want);
  char buf[96];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"bumper\":%s,\"model\":\"trace-e\"}",
           motorsBumperEnabled() ? "true" : "false");
  s.send(200, "application/json", buf);
}

static void handleSteer(WebServer &s) {
  corsHeaders(s);
  if (s.hasArg("kick")) STEER_KICK_MS = (unsigned long)s.arg("kick").toInt();
  if (s.hasArg("lock")) STEER_LOCK_MS = (unsigned long)s.arg("lock").toInt();
  if (s.hasArg("hold")) STEER_HOLD_PCT = constrain(s.arg("hold").toInt(), 0, 100);
  // Legacy both-sides args, then per-side overrides.
  if (s.hasArg("center")) {
    unsigned long v = (unsigned long)s.arg("center").toInt();
    STEER_CENTER_MS_L = v;
    STEER_CENTER_MS_R = v;
  }
  if (s.hasArg("center_pct")) {
    int v = constrain(s.arg("center_pct").toInt(), 0, 100);
    STEER_CENTER_PCT_L = v;
    STEER_CENTER_PCT_R = v;
  }
  if (s.hasArg("center_l")) STEER_CENTER_MS_L = (unsigned long)s.arg("center_l").toInt();
  if (s.hasArg("center_r")) STEER_CENTER_MS_R = (unsigned long)s.arg("center_r").toInt();
  if (s.hasArg("center_pct_l"))
    STEER_CENTER_PCT_L = constrain(s.arg("center_pct_l").toInt(), 0, 100);
  if (s.hasArg("center_pct_r"))
    STEER_CENTER_PCT_R = constrain(s.arg("center_pct_r").toInt(), 0, 100);
  if (s.hasArg("snap") || s.hasArg("snapback") || s.hasArg("on")) {
    String k = s.hasArg("snap") ? "snap" : (s.hasArg("snapback") ? "snapback" : "on");
    STEER_SNAPBACK = s.arg(k).toInt() != 0;
    // Re-arm sane pulse widths when switching back on after an off.
    if (STEER_SNAPBACK) {
      if (STEER_CENTER_MS_L == 0) STEER_CENTER_MS_L = 140;
      if (STEER_CENTER_MS_R == 0) STEER_CENTER_MS_R = 140;
    }
  }
  if (STEER_LOCK_MS < STEER_KICK_MS) STEER_LOCK_MS = STEER_KICK_MS;
  char buf[320];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"kick\":%lu,\"lock\":%lu,\"hold\":%d,"
           "\"center_l\":%lu,\"center_r\":%lu,"
           "\"center_pct_l\":%d,\"center_pct_r\":%d,"
           "\"center\":%lu,\"center_pct\":%d,"
           "\"snap\":%s,\"model\":\"trace-e\"}",
           STEER_KICK_MS, STEER_LOCK_MS, STEER_HOLD_PCT,
           STEER_CENTER_MS_L, STEER_CENTER_MS_R,
           STEER_CENTER_PCT_L, STEER_CENTER_PCT_R,
           STEER_CENTER_MS_L, STEER_CENTER_PCT_L,
           STEER_SNAPBACK ? "true" : "false");
  s.send(200, "application/json", buf);
}

static void handleTrim(WebServer &s) {
  corsHeaders(s);
  int left = 0, right = 0;
  motorsTrimGet(&left, &right);

  // Absolute set: ?left=97&right=104
  if (s.hasArg("left")) left = s.arg("left").toInt();
  if (s.hasArg("right")) right = s.arg("right").toInt();

  // Nudge while driving: ?nudge=l+ / l- / r+ / r-  or fix=left|right (pulling that way)
  if (s.hasArg("nudge")) {
    String n = s.arg("nudge");
    n.toLowerCase();
    if (n == "l+" || n == "left+" || n == "lfwd+") motorsTrimNudge(0, 2);
    else if (n == "l-" || n == "left-" || n == "lfwd-") motorsTrimNudge(0, -2);
    else if (n == "r+" || n == "right+") motorsTrimNudge(1, 2);
    else if (n == "r-" || n == "right-") motorsTrimNudge(1, -2);
  }
  if (s.hasArg("fix")) {
    // "I'm pulling LEFT" → cut left power. "pulling RIGHT" → cut right / boost left.
    String f = s.arg("fix");
    f.toLowerCase();
    if (f == "left" || f == "l") motorsTrimNudge(0, -2);
    else if (f == "right" || f == "r") motorsTrimNudge(1, -2);
  }
  if (s.hasArg("left") || s.hasArg("right")) {
    motorsTrimSet(left, right);
  }

  motorsTrimGet(&left, &right);
  // Re-apply current drive command so trim is felt immediately while holding W.
  if (!chaseEnabled() && (g_left != 0 || g_right != 0)) {
    driveLR(g_left, g_right);
  }
  char buf[128];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"trim_left\":%d,\"trim_right\":%d,\"model\":\"trace-e\"}",
           left, right);
  s.send(200, "application/json", buf);
}

static bool applyDriveCmd(int left, int right);

static void handleDrive(WebServer &s) {
  g_lastCmdMs = millis();
  corsHeaders(s);

  if (s.hasArg("cmd")) {
    String c = s.arg("cmd");
    c.toLowerCase();
    if (c == "stop" || c == "halt" || c == "coast") {
      if (chaseEnabled()) {
        g_camOk = chaseSetEnabled(false);
      }
      motorsCoast();
      g_left = 0;
      g_right = 0;
      s.send(200, "application/json",
             "{\"ok\":true,\"left\":0,\"right\":0,\"model\":\"trace-e\"}");
      return;
    }
  }

  int left = 0;
  int right = 0;
  bool hasLR = false;
  if (s.hasArg("left") || s.hasArg("right") || s.hasArg("l") || s.hasArg("r")) {
    hasLR = true;
    if (s.hasArg("left")) left = s.arg("left").toInt();
    else if (s.hasArg("l")) left = s.arg("l").toInt();
    if (s.hasArg("right")) right = s.arg("right").toInt();
    else if (s.hasArg("r")) right = s.arg("r").toInt();
  }

  if (!hasLR) {
    if (chaseEnabled()) {
      g_camOk = chaseSetEnabled(false);
    }
    motorsCoast();
    g_left = 0;
    g_right = 0;
    s.send(200, "application/json",
           "{\"ok\":true,\"left\":0,\"right\":0,\"model\":\"trace-e\"}");
    return;
  }

  if (chaseEnabled() && left == 0 && right == 0) {
    s.send(200, "application/json",
           "{\"ok\":true,\"left\":0,\"right\":0,\"chase\":true,\"model\":\"trace-e\"}");
    return;
  }

  if (applyDriveCmd(left, right)) {
    s.send(200, "application/json",
           "{\"ok\":true,\"left\":0,\"right\":0,\"bumper\":true,\"model\":\"trace-e\"}");
    return;
  }
  left = g_left;
  right = g_right;

  char buf[128];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"left\":%d,\"right\":%d,\"model\":\"trace-e\",\"fw\":\"%s\"}",
           left, right, MIC_FW_TAG);
  s.send(200, "application/json", buf);
}

static bool initCamera();

static bool restoreJpegCamera() {
  bool ok = initCamera();
  g_camOk = ok;
  return ok;
}

static bool initCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = PIN_CAM_Y2;
  config.pin_d1 = PIN_CAM_Y3;
  config.pin_d2 = PIN_CAM_Y4;
  config.pin_d3 = PIN_CAM_Y5;
  config.pin_d4 = PIN_CAM_Y6;
  config.pin_d5 = PIN_CAM_Y7;
  config.pin_d6 = PIN_CAM_Y8;
  config.pin_d7 = PIN_CAM_Y9;
  config.pin_xclk = PIN_CAM_XCLK;
  config.pin_pclk = PIN_CAM_PCLK;
  config.pin_vsync = PIN_CAM_VSYNC;
  config.pin_href = PIN_CAM_HREF;
  config.pin_sccb_sda = PIN_CAM_SIOD;
  config.pin_sccb_scl = PIN_CAM_SIOC;
  config.pin_pwdn = PIN_CAM_PWDN;
  config.pin_reset = PIN_CAM_RESET;
  config.xclk_freq_hz = CAM_XCLK_HZ;
  config.pixel_format = PIXFORMAT_JPEG;
  config.jpeg_quality = CAM_JPEG_Q;
  config.frame_size = CAM_FRAMESIZE;
  // PSRAM double-buffer + LATEST = no stale frames (critical for RC lag)
  if (psramFound()) {
    config.fb_count = 2;
    config.grab_mode = CAMERA_GRAB_LATEST;
    config.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    config.fb_count = 1;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    config.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init fail 0x%x\n", (unsigned)err);
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    // POFET OV2640 fisheye — PID should be 0x26
    Serial.printf("Cam sensor PID=0x%02x VER=0x%02x MID=0x%02x%02x\n",
                  s->id.PID, s->id.VER, s->id.MIDH, s->id.MIDL);
    // Chassis mounts the FPC upside-down, so the sensor un-flips it here. The HQ UI
    // must NOT also apply a CSS flip or the feed ends up upside down again.
    s->set_vflip(s, 1);
    s->set_hmirror(s, 0);
    s->set_framesize(s, CAM_FRAMESIZE);
    s->set_quality(s, CAM_JPEG_Q);
    // Indoor / darker play: slight lift without AEC2 washout (fisheye is grainy at night anyway)
    s->set_brightness(s, 1);
    s->set_contrast(s, 1);
    s->set_saturation(s, 0);
    s->set_sharpness(s, 1);
    s->set_special_effect(s, 0);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_wb_mode(s, 0);
    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 0);
    s->set_ae_level(s, 1);       // +1 for dim rooms
    s->set_aec_value(s, 500);
    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)3);  // allow more gain in dark (was 1)
    s->set_bpc(s, 0);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);   // lens correction helps fisheye edges a bit
    s->set_dcw(s, 1);
  }
  Serial.printf("Camera OK POFET-fisheye jpeg_q=%d VGA xclk=%dHz grab=LATEST\n",
                CAM_JPEG_Q, CAM_XCLK_HZ);
  return true;
}

static void handleCameraQuality(WebServer &s) {
  corsHeaders(s);
  if (!g_camOk) {
    s.send(503, "application/json", "{\"ok\":false,\"error\":\"camera not ready\"}");
    return;
  }
  sensor_t *sens = esp_camera_sensor_get();
  if (!sens) {
    s.send(503, "application/json", "{\"ok\":false,\"error\":\"no sensor\"}");
    return;
  }
  int bright = s.hasArg("brightness") ? s.arg("brightness").toInt() : 1;
  int ae = s.hasArg("ae_level") ? s.arg("ae_level").toInt() : 1;
  int vflip = s.hasArg("vflip") ? s.arg("vflip").toInt() : -1;
  int hmirror = s.hasArg("hmirror") ? s.arg("hmirror").toInt() : -1;
  int night = s.hasArg("night") ? s.arg("night").toInt() : -1;
  if (bright < -2) bright = -2;
  if (bright > 2) bright = 2;
  if (ae < -2) ae = -2;
  if (ae > 2) ae = 2;
  if (night == 1) {
    bright = 1;
    ae = 2;
  } else if (night == 0) {
    bright = 0;
    ae = 0;
  }
  sens->set_brightness(sens, bright);
  sens->set_ae_level(sens, ae);
  sens->set_contrast(sens, 1);
  sens->set_saturation(sens, 0);
  sens->set_sharpness(sens, 1);
  sens->set_exposure_ctrl(sens, 1);
  sens->set_aec2(sens, 0);
  sens->set_gain_ctrl(sens, 1);
  sens->set_gainceiling(sens, (gainceiling_t)(night == 1 ? 4 : 3));
  if (vflip >= 0) sens->set_vflip(sens, vflip ? 1 : 0);
  if (hmirror >= 0) sens->set_hmirror(sens, hmirror ? 1 : 0);
  int framesize = s.hasArg("framesize") ? s.arg("framesize").toInt() : -1;
  int quality = s.hasArg("quality") ? s.arg("quality").toInt() : -1;
  if (framesize >= 0 && framesize <= 13) {
    sens->set_framesize(sens, (framesize_t)framesize);
  }
  if (quality >= 4 && quality <= 40) {
    sens->set_quality(sens, quality);
  }
  char buf[240];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"brightness\":%d,\"ae_level\":%d,\"vflip\":%d,\"hmirror\":%d,\"night\":%d,\"framesize\":%d,\"quality\":%d,\"note\":\"POFET fisheye tiny\"}",
           bright, ae,
           sens->status.vflip, sens->status.hmirror, night,
           (int)sens->status.framesize, (int)sens->status.quality);
  s.send(200, "application/json", buf);
}

/** Dedicated :82 MJPEG — drop dead clients fast so accept() isn't wedged. */
static void streamTask(void *) {
  streamServer.begin();
  streamServer.setNoDelay(true);
  Serial.println("MJPEG :82/stream (dedicated task, accept())");
  for (;;) {
    // ESP32 Arduino 3.x: available() is broken/deprecated — use accept()
    WiFiClient client = streamServer.accept();
    if (!client) {
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }
    client.setNoDelay(true);
    client.setTimeout(1000);
    unsigned long t0 = millis();
    int blankRun = 0;
    while (client.connected() && (millis() - t0) < 800) {
      if (!client.available()) {
        vTaskDelay(pdMS_TO_TICKS(2));
        continue;
      }
      int c = client.read();
      if (c < 0) break;
      if (c == '\n') {
        blankRun++;
        if (blankRun >= 2) break;
      } else if (c != '\r') {
        blankRun = 0;
      }
    }
    if (!g_camOk) {
      client.print(F("HTTP/1.1 503 Unavailable\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\ncamera not ready"));
      client.stop();
      continue;
    }
    if (!client.connected()) {
      client.stop();
      continue;
    }
    client.print(F("HTTP/1.1 200 OK\r\n"
                   "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
                   "Access-Control-Allow-Origin: *\r\n"
                   "Cache-Control: no-store, no-cache\r\n"
                   "Connection: close\r\n\r\n"));
    g_camStreaming = true;
    unsigned long lastOk = millis();
    while (client.connected()) {
      if ((millis() - lastOk) > 4000UL) break;
      if (chaseEnabled()) {
        uint8_t *jpg = nullptr;
        size_t len = 0;
        if (!chaseCloneJpeg(&jpg, &len)) {
          vTaskDelay(pdMS_TO_TICKS(20));
          continue;
        }
        char hdr[96];
        int hl = snprintf(hdr, sizeof(hdr),
                         "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                         (unsigned)len);
        bool ok = false;
        if (hl > 0 &&
            client.write((const uint8_t *)hdr, (size_t)hl) == (size_t)hl &&
            client.write(jpg, len) == len &&
            client.write((const uint8_t *)"\r\n", 2) == 2) {
          ok = true;
          lastOk = millis();
        }
        free(jpg);
        if (!ok || !client.connected()) break;
        vTaskDelay(pdMS_TO_TICKS(30));
        continue;
      }
      camera_fb_t *fb = esp_camera_fb_get();
      if (!fb) {
        vTaskDelay(pdMS_TO_TICKS(5));
        continue;
      }
      char hdr[96];
      int hl = snprintf(hdr, sizeof(hdr),
                       "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                       (unsigned)fb->len);
      bool ok = false;
      if (hl > 0 &&
          client.write((const uint8_t *)hdr, (size_t)hl) == (size_t)hl &&
          client.write(fb->buf, fb->len) == fb->len &&
          client.write((const uint8_t *)"\r\n", 2) == 2) {
        ok = true;
        lastOk = millis();
      }
      esp_camera_fb_return(fb);
      if (!ok || !client.connected()) break;
      vTaskDelay(pdMS_TO_TICKS(1));
    }
    g_camStreaming = false;
    client.stop();
    // Drain any backlog connection that piled up while we were busy
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

// Shared by HTTP /api/drive and the UDP control link.
// left = A rack steer, right = B rear drive. Returns true if bumper blocked.
// --- Rack steer shaping -------------------------------------------------
// The rack is a positional DC motor with no feedback: it reaches full lock in
// a fraction of a second and then just stalls. Holding 100% into that stall is
// what cooks these cheap hobby motors, so the steer channel is shaped:
//   0..KICK_MS      full power  (snap it over)
//   ..LOCK_MS       HOLD_PCT    (enough to stay at lock, not stall hard)
//   after LOCK_MS   0           (rack is already there — stop heating it)
// On release, a short opposite pulse rougly re-centres. There is no position
// sensor, so this is timed dead-reckoning, not a true auto-straight.
static int g_reqSteer = 0;   // what the UI/brain asked for
static int g_reqDrive = 0;
static int g_steerDir = 0;   // sign of the active steer hold
static int g_steerMag = 0;   // |requested| for this hold — lets A and D run at
                             // different sensitivities instead of a fixed 100
static unsigned long g_steerStartMs = 0;
static int g_centerDir = 0;  // opposite pulse direction while re-centring
static unsigned long g_centerUntilMs = 0;
static int g_centerPct = 0;  // strength of the active burst (per released side)

static int shapedSteer() {
  const unsigned long now = millis();
  if (g_steerDir != 0) {
    const unsigned long held = now - g_steerStartMs;
    if (held < STEER_KICK_MS) return g_steerDir * g_steerMag;
    // Keep hold power while the key is down. Cutting to 0 made A/D die after
    // ~400ms and the rack never finished the snap.
    return g_steerDir * (g_steerMag * STEER_HOLD_PCT) / 100;
  }
  if (g_centerDir != 0) {
    if (now < g_centerUntilMs) return g_centerDir * g_centerPct;
    g_centerDir = 0;
  }
  return 0;
}

static void pushShapedDrive() {
  driveLR(shapedSteer(), g_reqDrive);
}

// Re-apply on a timer so kick→hold→cut and the centring pulse still advance
// when the UI is just repeating the same held command.
static void steerTick() {
  if (g_lastCmdMs == 0) {  // failsafe already coasted — don't re-energise anything
    g_steerDir = 0;
    g_centerDir = 0;
    return;
  }
  if (g_steerDir != 0 || g_centerDir != 0) pushShapedDrive();
}

static bool applyDriveCmd(int left, int right) {
  if (left > 100) left = 100;
  if (left < -100) left = -100;
  if (right > 100) right = 100;
  if (right < -100) right = -100;

  // Non-zero WASD cancels chase. Zero keepalives leave chase alone.
  if (chaseEnabled()) {
    if (left == 0 && right == 0) return false;
    g_camOk = chaseSetEnabled(false);
  }

  const int dir = (left > 0) ? 1 : ((left < 0) ? -1 : 0);
  if (dir != g_steerDir) {
    if (dir != 0) {
      g_steerDir = dir;          // new steer press (or direction change) → fresh kick
      g_steerMag = (left < 0) ? -left : left;  // carry the UI's per-side magnitude
      g_steerStartMs = millis();
      g_centerDir = 0;
    } else {
      // Timed opposite pulse (dead-reckoning). No servo pot. Strength and
      // duration come from whichever side was just released.
      const bool wasLeft = (g_steerDir < 0);
      const int pct = wasLeft ? STEER_CENTER_PCT_L : STEER_CENTER_PCT_R;
      const unsigned long ms = wasLeft ? STEER_CENTER_MS_L : STEER_CENTER_MS_R;
      if (STEER_SNAPBACK && ms > 0 && pct > 0 && g_steerDir != 0) {
        g_centerDir = -g_steerDir;
        g_centerPct = pct;
        g_centerUntilMs = millis() + ms;
      } else {
        g_centerDir = 0;
      }
      g_steerDir = 0;
    }
  }
  g_reqSteer = left;
  g_reqDrive = right;

  pushShapedDrive();
  g_lastCmdMs = millis();
  if (motorsBumperBlocked()) {
    g_left = 0;
    g_right = right > 0 ? 0 : right;
    return true;
  }
  g_left = left;
  g_right = right;
  return false;
}

// Latest-command-wins: drain the whole datagram backlog so a burst of
// keepalives can never queue up stale steering behind the newest one.
static void driveUdpTick() {
  int steer = 0;
  int drive = 0;
  bool got = false;
  uint8_t buf[8];
  for (int guard = 0; guard < 16; guard++) {
    const int sz = driveUdp.parsePacket();
    if (sz <= 0) break;
    const int n = driveUdp.read(buf, sizeof(buf));
    if (n >= 4 && buf[0] == 'T' && buf[1] == 'E') {
      steer = (int8_t)buf[2];
      drive = (int8_t)buf[3];
      got = true;
    } else if (n >= 2) {
      steer = (int8_t)buf[0];
      drive = (int8_t)buf[1];
      got = true;
    }
  }
  if (!got) return;
  g_udpPackets++;
  applyDriveCmd(steer, drive);
}

static void driveBridgeTask(void *) {
  for (;;) {
    driveSrv.handleClient();
    driveUdpTick();
    steerTick();
    driveFailsafeTick();
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

static bool wifiConnectSta() {
#if HAS_WIFI_CONFIG
  if (WIFI_SSID[0] == '\0') {
    Serial.println("WiFi: wifi_config.h has empty SSID — skipping STA");
    return false;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("trace-e-bot");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("WiFi: connecting to %s", WIFI_SSID);
  const unsigned long deadline = millis() + 20000UL;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    g_ip = WiFi.localIP().toString();
    WiFi.setSleep(false);
    Serial.printf("WiFi: OK %s (sleep off)\n", g_ip.c_str());
    return true;
  }
  Serial.println("WiFi: failed (continuing offline)");
  return false;
#else
  Serial.println("WiFi: no wifi_config.h — copy wifi_config.h.example → wifi_config.h");
  return false;
#endif
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("========================================");
  Serial.println("  Trace-E Bot");
  Serial.printf("  model=%s  mic_fw=%s\n", MODEL_ID, MIC_FW_TAG);
  Serial.println("========================================");

  motorsInit();
  ampInit();

  pinMode(PIN_MIC, INPUT);
  pinMode(PIN_TRIG, OUTPUT);
  digitalWrite(PIN_TRIG, LOW);
  pinMode(PIN_ECHO, INPUT);
  // Pretend headlights — off at boot; UI owns on/off + brightness.
  ledcAttachChannel(PIN_HEADLIGHTS, 5000, 8, 7);
  setHeadlightBrightness(0);
  // SR04 hard bumper: blocks net-forward WASD + chase (reverse still OK).
  motorsSetBumper([]() -> float { return ultrasonicCachedCm(); }, 10.0f);

  g_wifiOk = wifiConnectSta();
  g_camOk = initCamera();

  chaseInit();
  chaseSetRestoreFn(restoreJpegCamera);

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/drive", HTTP_GET, []() { handleDrive(server); });
  server.on("/api/drive", HTTP_POST, []() { handleDrive(server); });
  server.on("/api/drive", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/api/chase", HTTP_GET, []() { handleChase(server); });
  server.on("/api/chase", HTTP_POST, []() { handleChase(server); });
  server.on("/api/chase", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/api/headlights", HTTP_GET, []() { handleHeadlights(server); });
  server.on("/api/headlights", HTTP_POST, []() { handleHeadlights(server); });
  server.on("/api/headlights", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/api/lights", HTTP_GET, []() { handleHeadlights(server); });
  server.on("/api/lights", HTTP_POST, []() { handleHeadlights(server); });
  server.on("/api/lights", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/api/bumper", HTTP_GET, []() { handleBumper(server); });
  server.on("/api/bumper", HTTP_POST, []() { handleBumper(server); });
  server.on("/api/bumper", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/api/steer", HTTP_GET, []() { handleSteer(server); });
  server.on("/api/steer", HTTP_POST, []() { handleSteer(server); });
  server.on("/api/steer", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/api/trim", HTTP_GET, []() { handleTrim(server); });
  server.on("/api/trim", HTTP_POST, []() { handleTrim(server); });
  server.on("/api/trim", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/api/status", HTTP_GET, []() { handleStatus(server); });
  server.on("/api/status", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/capture", HTTP_GET, []() { handleCapture(server); });
  server.on("/api/capture", HTTP_GET, []() { handleCapture(server); });
  server.on("/api/camera_quality", HTTP_GET, []() { handleCameraQuality(server); });
  server.on("/api/camera_orient", HTTP_GET, []() { handleCameraQuality(server); });
  server.on("/api/reboot", HTTP_GET, []() { handleReboot(server); });
  server.on("/api/mic_wav", HTTP_GET, []() { handleMicWav(server); });
  server.begin();

  driveSrv.on("/api/drive", HTTP_GET, []() { handleDrive(driveSrv); });
  driveSrv.on("/api/drive", HTTP_POST, []() { handleDrive(driveSrv); });
  driveSrv.on("/api/drive", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/api/chase", HTTP_GET, []() { handleChase(driveSrv); });
  driveSrv.on("/api/chase", HTTP_POST, []() { handleChase(driveSrv); });
  driveSrv.on("/api/chase", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/api/headlights", HTTP_GET, []() { handleHeadlights(driveSrv); });
  driveSrv.on("/api/headlights", HTTP_POST, []() { handleHeadlights(driveSrv); });
  driveSrv.on("/api/headlights", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/api/lights", HTTP_GET, []() { handleHeadlights(driveSrv); });
  driveSrv.on("/api/lights", HTTP_POST, []() { handleHeadlights(driveSrv); });
  driveSrv.on("/api/lights", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/api/bumper", HTTP_GET, []() { handleBumper(driveSrv); });
  driveSrv.on("/api/bumper", HTTP_POST, []() { handleBumper(driveSrv); });
  driveSrv.on("/api/bumper", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/api/steer", HTTP_GET, []() { handleSteer(driveSrv); });
  driveSrv.on("/api/steer", HTTP_POST, []() { handleSteer(driveSrv); });
  driveSrv.on("/api/steer", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/api/trim", HTTP_GET, []() { handleTrim(driveSrv); });
  driveSrv.on("/api/trim", HTTP_POST, []() { handleTrim(driveSrv); });
  driveSrv.on("/api/trim", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/api/status", HTTP_GET, []() { handleStatus(driveSrv); });
  driveSrv.on("/api/status", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  ampRegisterRoutes(driveSrv);
  driveSrv.begin();
  driveUdp.begin(DRIVE_UDP_PORT);

  // Drive and cam are the two top jobs. 8765 is drive-only (no capture/mic).
  xTaskCreatePinnedToCore(driveBridgeTask, "drive8765", 12288, nullptr, 5, nullptr, 1);
  xTaskCreatePinnedToCore(chaseTask, "chase", 12288, nullptr, 4, nullptr, 1);
  xTaskCreatePinnedToCore(streamTask, "mjpeg82", 10240, nullptr, 4, nullptr, 0);

  Serial.println("HTTP :80 /api/status /capture /api/mic_wav /api/chase");
  Serial.println("Drive :8765 /api/drive /api/chase");
  Serial.println("Amp   :8765 /api/play_wav /api/play_url /api/stop_audio /api/volume");
  Serial.println("Cam   :82/stream");
  Serial.println("Chase : local bright-orange (on=1) — no Wi-Fi in drive loop");
  if (g_wifiOk) {
    Serial.printf("Open http://%s/api/status\n", g_ip.c_str());
    Serial.printf("Cam  http://%s:82/stream\n", g_ip.c_str());
    Serial.printf("Snap http://%s/capture\n", g_ip.c_str());
  }
  Serial.println("Bumper: SR04 stop forward <10cm (fail-open, reverse OK)");
  Serial.println("Boot complete — motors coast · amp silent (no boot tone).");
}

void loop() {
  server.handleClient();
  driveFailsafeTick();
  delay(2);
}
