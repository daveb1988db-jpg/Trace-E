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
#include <esp_camera.h>
#include "pins.h"
#include "motors.h"
#include "amp.h"

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
static const char *MIC_FW_TAG = "trace-e-drive-cam-2";
static const char *FW_NAME = "Trace-E Bot";

static const unsigned long DRIVE_FAILSAFE_MS = 450;
static const int CAM_JPEG_Q = 12;

static WebServer server(80);
static WebServer driveSrv(8765);
static WiFiServer streamServer(82);

static bool g_wifiOk = false;
static bool g_camOk = false;
static String g_ip = "";
static int g_left = 0;
static int g_right = 0;
static unsigned long g_lastCmdMs = 0;
static volatile bool g_camStreaming = false;

static void corsHeaders(WebServer &s) {
  s.sendHeader("Access-Control-Allow-Origin", "*");
  s.sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  s.sendHeader("Access-Control-Allow-Headers", "*");
  s.sendHeader("Cache-Control", "no-store");
}

static String statusJson() {
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
  body += "\"left\":";
  body += String(g_left);
  body += ",";
  body += "\"right\":";
  body += String(g_right);
  body += ",";
  body += "\"stream\":\"http://";
  body += ip;
  body += ":82/stream\",";
  body += "\"capture\":\"http://";
  body += ip;
  body += "/capture\",";
  body += "\"drive\":\"http://";
  body += ip;
  body += ":8765/api/drive\"";
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
  if (!g_camOk) {
    s.send(503, "text/plain", "camera not ready");
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

static void driveFailsafeTick() {
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

static void handleDrive(WebServer &s) {
  g_lastCmdMs = millis();
  corsHeaders(s);

  if (s.hasArg("cmd")) {
    String c = s.arg("cmd");
    c.toLowerCase();
    if (c == "stop" || c == "halt" || c == "coast") {
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
    motorsCoast();
    g_left = 0;
    g_right = 0;
    s.send(200, "application/json",
           "{\"ok\":true,\"left\":0,\"right\":0,\"model\":\"trace-e\"}");
    return;
  }

  if (left > 100) left = 100;
  if (left < -100) left = -100;
  if (right > 100) right = 100;
  if (right < -100) right = -100;

  driveLR(left, right);
  g_left = left;
  g_right = right;

  char buf[128];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"left\":%d,\"right\":%d,\"model\":\"trace-e\",\"fw\":\"%s\"}",
           left, right, MIC_FW_TAG);
  s.send(200, "application/json", buf);
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
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.jpeg_quality = CAM_JPEG_Q;
  config.frame_size = FRAMESIZE_QVGA;
  if (psramFound()) {
    config.fb_count = 2;
    config.grab_mode = CAMERA_GRAB_LATEST;
    config.fb_location = CAMERA_FB_IN_PSRAM;
    config.frame_size = FRAMESIZE_VGA;
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
    // Freenove FPC on Trace desk mounts upside-down — sensor vflip for upright MJPEG
    s->set_vflip(s, 1);
    s->set_hmirror(s, 0);
  }
  Serial.println("Camera OK");
  return true;
}

/** Dedicated :82 MJPEG — non-blocking header drain (peanut pattern). */
static void streamTask(void *) {
  streamServer.begin();
  streamServer.setNoDelay(true);
  Serial.println("MJPEG :82/stream (dedicated task, accept())");
  for (;;) {
    // ESP32 Arduino 3.x: available() is broken/deprecated — use accept()
    WiFiClient client = streamServer.accept();
    if (!client) {
      vTaskDelay(pdMS_TO_TICKS(8));
      continue;
    }
    client.setNoDelay(true);
    client.setTimeout(50);
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
      driveFailsafeTick();
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
      vTaskDelay(pdMS_TO_TICKS(5));
    }
    g_camStreaming = false;
    client.stop();
  }
}

static void driveBridgeTask(void *) {
  for (;;) {
    driveSrv.handleClient();
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
    Serial.printf("WiFi: OK %s\n", g_ip.c_str());
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

  g_wifiOk = wifiConnectSta();
  g_camOk = initCamera();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/status", HTTP_GET, []() { handleStatus(server); });
  server.on("/api/status", HTTP_OPTIONS, []() { handleOptions(server); });
  server.on("/capture", HTTP_GET, []() { handleCapture(server); });
  server.on("/api/capture", HTTP_GET, []() { handleCapture(server); });
  server.on("/api/reboot", HTTP_GET, []() { handleReboot(server); });
  server.begin();

  driveSrv.on("/api/drive", HTTP_GET, []() { handleDrive(driveSrv); });
  driveSrv.on("/api/drive", HTTP_POST, []() { handleDrive(driveSrv); });
  driveSrv.on("/api/drive", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/api/status", HTTP_GET, []() { handleStatus(driveSrv); });
  driveSrv.on("/api/status", HTTP_OPTIONS, []() { handleOptions(driveSrv); });
  driveSrv.on("/capture", HTTP_GET, []() { handleCapture(driveSrv); });
  driveSrv.on("/api/capture", HTTP_GET, []() { handleCapture(driveSrv); });
  driveSrv.on("/api/reboot", HTTP_GET, []() { handleReboot(driveSrv); });
  ampRegisterRoutes(driveSrv);
  driveSrv.begin();

  xTaskCreatePinnedToCore(driveBridgeTask, "drive8765", 6144, nullptr, 4, nullptr, 1);
  xTaskCreatePinnedToCore(streamTask, "mjpeg82", 10240, nullptr, 2, nullptr, 0);

  Serial.println("HTTP :80 /api/status /capture");
  Serial.println("Drive :8765 /api/drive /capture /api/reboot");
  Serial.println("Amp   :8765 /api/play_wav /api/play_url /api/stop_audio");
  Serial.println("Cam   :82/stream");
  if (g_wifiOk) {
    Serial.printf("Open http://%s/api/status\n", g_ip.c_str());
    Serial.printf("Cam  http://%s:82/stream\n", g_ip.c_str());
    Serial.printf("Snap http://%s/capture\n", g_ip.c_str());
  }
  Serial.println("Boot complete — motors coast · amp silent (no boot tone).");
}

void loop() {
  server.handleClient();
  driveFailsafeTick();
  delay(2);
}
