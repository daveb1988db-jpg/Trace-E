#include "chase.h"

#include <string.h>
#include <esp_camera.h>
#include "img_converters.h"
#include "pins.h"
#include "motors.h"

// Work at QVGA after JPEG decode (VGA JPEG → scale 2x).
static const int CHASE_W = 320;
static const int CHASE_H = 240;
// Slightly looser so rag locks before "slap the lens", still reject wood/skin.
static const int ORANGE_R_MIN = 145;
static const int ORANGE_G_MIN = 50;
static const int ORANGE_G_MAX = 175;
static const int ORANGE_B_MAX = 80;
static const int ORANGE_RB_MIN = 60;
static const int ORANGE_RG_MIN = 20;
static const int ORANGE_RG_MAX = 100;
static const int ORANGE_GB_MIN = 12;
static const int MIN_BLOB_PIXELS = 70;
static const int ARRIVE_BLOB_PIXELS = 38000;
static const int DRIVE_SPEED = 100;
static const float STEER_DEADBAND = 0.10f;
static const int LOCK_FRAMES = 3;
static const int CHASE_US_STOP_CM = 10;   // hard stop
static const int CHASE_US_SLOW_CM = 25;   // crawl when close
static const int TRACK_GATE = 70;
static const int MISS_KEEP = 6;           // brief hold, then COAST (don't keep turning into walls)
static const float TRACK_SMOOTH = 0.65f;

static volatile bool g_on = false;
static volatile bool g_found = false;
static volatile int g_cx = 0;
static volatile int g_pixels = 0;
static volatile float g_usCm = -1.0f;
static volatile int g_cmdL = 0;
static volatile int g_cmdR = 0;

static uint8_t *g_jpg = nullptr;
static size_t g_jpgLen = 0;
static portMUX_TYPE g_jpgMux = portMUX_INITIALIZER_UNLOCKED;
static ChaseCamRestoreFn g_restore = nullptr;  // unused — kept for API compat
static int g_lock = 0;
static uint8_t *g_rgb = nullptr;
static size_t g_rgbCap = 0;
static float g_trackCx = -1.0f;
static int g_miss = 0;
static int g_usFail = 0;
static unsigned long g_usLastMs = 0;

void chaseSetRestoreFn(ChaseCamRestoreFn fn) { g_restore = fn; (void)g_restore; }

bool chaseEnabled() { return g_on; }

void chaseStatus(bool *found, int *cx, int *pixels, float *usCm) {
  if (found) *found = g_found;
  if (cx) *cx = g_cx;
  if (pixels) *pixels = g_pixels;
  if (usCm) *usCm = g_usCm;
}

int chaseCmdLeft() { return g_cmdL; }
int chaseCmdRight() { return g_cmdR; }

bool chaseCloneJpeg(uint8_t **out, size_t *outLen) {
  if (!out || !outLen) return false;
  *out = nullptr;
  *outLen = 0;
  portENTER_CRITICAL(&g_jpgMux);
  if (!g_jpg || g_jpgLen < 100) {
    portEXIT_CRITICAL(&g_jpgMux);
    return false;
  }
  size_t n = g_jpgLen;
  uint8_t *p = (uint8_t *)ps_malloc(n);
  if (!p) p = (uint8_t *)malloc(n);
  if (!p) {
    portEXIT_CRITICAL(&g_jpgMux);
    return false;
  }
  memcpy(p, g_jpg, n);
  portEXIT_CRITICAL(&g_jpgMux);
  *out = p;
  *outLen = n;
  return true;
}

static float sr04Cm() {
  // Rate-limit so chase + /api/status don't starve each other on pulseIn.
  unsigned long now = millis();
  if (g_usLastMs != 0 && (now - g_usLastMs) < 45UL) {
    return g_usCm;
  }
  g_usLastMs = now;

  long best = -1;
  for (int i = 0; i < 2; i++) {
    digitalWrite(PIN_TRIG, LOW);
    delayMicroseconds(2);
    digitalWrite(PIN_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_TRIG, LOW);
    // ~3m max; skirting / close walls need a reliable echo window
    unsigned long us = pulseIn(PIN_ECHO, HIGH, 18000UL);
    if (us == 0) continue;
    long cm = (long)(us / 58UL);
    if (cm < 2 || cm > 300) continue;
    if (best < 0 || cm < best) best = cm;  // prefer nearer hit
  }
  return best > 0 ? (float)best : -1.0f;
}

static inline bool isOrangePixel(uint8_t r, uint8_t g, uint8_t b) {
  if (r < ORANGE_R_MIN) return false;
  if (g < ORANGE_G_MIN || g > ORANGE_G_MAX) return false;
  if (b > ORANGE_B_MAX) return false;
  if (r <= g || r <= b) return false;
  int rb = (int)r - (int)b;
  int rg = (int)r - (int)g;
  int gb = (int)g - (int)b;
  return rb >= ORANGE_RB_MIN && rg >= ORANGE_RG_MIN && rg <= ORANGE_RG_MAX && gb >= ORANGE_GB_MIN;
}

static bool findOrangeBlobRgb(const uint16_t *pixels, int w, int h, int preferCx,
                              int *outCx, int *outPixels) {
  // Column histogram — pick the strongest orange peak (rejects scattered false hits).
  static uint16_t col[CHASE_W];
  if (w > CHASE_W) w = CHASE_W;
  for (int x = 0; x < w; x++) col[x] = 0;

  int x0 = 0, x1 = w;
  if (preferCx >= 0) {
    x0 = preferCx - TRACK_GATE;
    x1 = preferCx + TRACK_GATE;
    if (x0 < 0) x0 = 0;
    if (x1 > w) x1 = w;
  }

  long sumX = 0;
  int count = 0;
  for (int y = 0; y < h; y += 2) {
    for (int x = x0; x < x1; x += 2) {
      uint16_t px = pixels[y * w + x];
      uint8_t r = ((px >> 11) & 0x1F) << 3;
      uint8_t g = ((px >> 5) & 0x3F) << 2;
      uint8_t b = (px & 0x1F) << 3;
      if (!isOrangePixel(r, g, b)) continue;
      col[x]++;
      sumX += x;
      count++;
    }
  }

  // If gated search too weak, fall back to full frame once.
  if (preferCx >= 0 && count < (MIN_BLOB_PIXELS / 4)) {
    return findOrangeBlobRgb(pixels, w, h, -1, outCx, outPixels);
  }
  if (count < (MIN_BLOB_PIXELS / 4)) return false;

  // Peak column (smoothed over ±2) so we stick to the densest rag, not noise.
  int bestX = 0;
  int bestScore = -1;
  for (int x = 2; x < w - 2; x++) {
    int score = (int)col[x - 2] + (int)col[x - 1] + (int)col[x] + (int)col[x + 1] + (int)col[x + 2];
    if (score > bestScore) {
      bestScore = score;
      bestX = x;
    }
  }
  if (bestScore < (MIN_BLOB_PIXELS / 8)) return false;

  // Centroid only around the peak (±gate) so wood elsewhere can't pull the mean.
  int p0 = bestX - TRACK_GATE / 2;
  int p1 = bestX + TRACK_GATE / 2;
  if (p0 < 0) p0 = 0;
  if (p1 > w) p1 = w;
  long cSum = 0;
  int cN = 0;
  for (int x = p0; x < p1; x++) {
    cSum += (long)x * (long)col[x];
    cN += col[x];
  }
  if (cN < (MIN_BLOB_PIXELS / 8)) {
    *outCx = bestX;
    *outPixels = count * 4;
    return true;
  }
  *outCx = (int)(cSum / cN);
  *outPixels = count * 4;
  return true;
}

static void publishJpegCopy(const uint8_t *jpg, size_t len) {
  if (!jpg || len < 100) return;
  uint8_t *copy = (uint8_t *)ps_malloc(len);
  if (!copy) copy = (uint8_t *)malloc(len);
  if (!copy) return;
  memcpy(copy, jpg, len);
  portENTER_CRITICAL(&g_jpgMux);
  if (g_jpg) free(g_jpg);
  g_jpg = copy;
  g_jpgLen = len;
  portEXIT_CRITICAL(&g_jpgMux);
}

static void chaseSensorTweaks(bool on) {
  sensor_t *s = esp_camera_sensor_get();
  if (!s) return;
  // Keep full VGA + sharp JPEG while chasing (blob math downscales in software).
  s->set_framesize(s, FRAMESIZE_VGA);
  s->set_quality(s, 10);
  s->set_sharpness(s, 1);
  if (on) {
    s->set_saturation(s, 1);  // sat=2 made wood/skin look orange
  } else {
    s->set_saturation(s, 1);
  }
}

void chaseInit() {}

bool chaseSetEnabled(bool on) {
  if (!on) {
    g_on = false;
    vTaskDelay(pdMS_TO_TICKS(30));
    motorsCoast();
    g_found = false;
    g_pixels = 0;
    g_lock = 0;
    g_trackCx = -1.0f;
    g_miss = 0;
    portENTER_CRITICAL(&g_jpgMux);
    if (g_jpg) {
      free(g_jpg);
      g_jpg = nullptr;
      g_jpgLen = 0;
    }
    portEXIT_CRITICAL(&g_jpgMux);
    chaseSensorTweaks(false);
    Serial.println("Chase OFF (JPEG cam kept)");
    return true;
  }

  // Re-arm even if already on (STOP → CHASE again must always work).
  g_on = false;
  vTaskDelay(pdMS_TO_TICKS(20));
  motorsCoast();
  g_found = false;
  g_pixels = 0;
  g_lock = 0;
  g_trackCx = -1.0f;
  g_miss = 0;

  const size_t need = (size_t)CHASE_W * (size_t)CHASE_H * 2u;
  if (!g_rgb || g_rgbCap < need) {
    if (g_rgb) free(g_rgb);
    g_rgb = (uint8_t *)ps_malloc(need);
    if (!g_rgb) g_rgb = (uint8_t *)malloc(need);
    g_rgbCap = g_rgb ? need : 0;
  }
  if (!g_rgb) {
    Serial.println("Chase ON fail — no RGB buffer");
    return false;
  }

  chaseSensorTweaks(true);
  g_on = true;
  Serial.println("Chase ON — JPEG decode, no cam reinit");
  return true;
}

void chaseTask(void *pv) {
  (void)pv;
  for (;;) {
    if (!g_on) {
      g_lock = 0;
      g_trackCx = -1.0f;
      g_miss = 0;
      vTaskDelay(pdMS_TO_TICKS(30));
      continue;
    }

    float dist = sr04Cm();
    g_usCm = dist;
    // Fail-CLOSED: no echo / flaky SR04 must NOT mean "clear path".
    if (dist < 0) {
      if (++g_usFail >= 2) {
        motorsCoast();
        g_cmdL = g_cmdR = 0;
        vTaskDelay(pdMS_TO_TICKS(40));
        continue;
      }
    } else {
      g_usFail = 0;
      if (dist < (float)CHASE_US_STOP_CM) {
        motorsCoast();
        g_cmdL = g_cmdR = 0;
        g_found = false;
        g_pixels = 0;
        g_lock = 0;
        g_trackCx = -1.0f;
        g_miss = 0;
        vTaskDelay(pdMS_TO_TICKS(50));
        continue;
      }
    }

    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb || !fb->buf || fb->len < 100) {
      if (fb) esp_camera_fb_return(fb);
      vTaskDelay(pdMS_TO_TICKS(15));
      continue;
    }

    // FPV preview = same JPEG the chase loop just grabbed.
    publishJpegCopy(fb->buf, fb->len);

    bool found = false;
    int cx = 0, pixels = 0;
    int prefer = (g_trackCx >= 0.0f) ? (int)(g_trackCx + 0.5f) : -1;
    if (fb->format == PIXFORMAT_JPEG && g_rgb) {
      esp_jpeg_image_scale_t scale = JPG_SCALE_2X;
      if (fb->width <= 320 && fb->height <= 240) scale = JPG_SCALE_NONE;
      if (jpg2rgb565(fb->buf, fb->len, g_rgb, scale)) {
        found = findOrangeBlobRgb((const uint16_t *)g_rgb, CHASE_W, CHASE_H, prefer, &cx, &pixels);
      }
    } else if (fb->format == PIXFORMAT_RGB565) {
      found = findOrangeBlobRgb((const uint16_t *)fb->buf, fb->width, fb->height, prefer, &cx, &pixels);
    }

    esp_camera_fb_return(fb);

    if (found) {
      // Reject jumps far from the current lock (false orange elsewhere).
      if (g_trackCx >= 0.0f && abs(cx - (int)(g_trackCx + 0.5f)) > (TRACK_GATE + 20) &&
          pixels < (MIN_BLOB_PIXELS * 2)) {
        found = false;
      }
    }

    if (found) {
      g_miss = 0;
      if (g_trackCx < 0.0f) g_trackCx = (float)cx;
      else g_trackCx = TRACK_SMOOTH * g_trackCx + (1.0f - TRACK_SMOOTH) * (float)cx;
      cx = (int)(g_trackCx + 0.5f);
      g_lock++;
    } else if (g_trackCx >= 0.0f && g_miss < MISS_KEEP) {
      // Lost rag briefly — COAST (do not keep turning into skirting/walls).
      g_miss++;
      g_found = false;
      g_cmdL = g_cmdR = 0;
      motorsCoast();
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    } else {
      g_lock = 0;
      g_trackCx = -1.0f;
      g_miss = 0;
      g_found = false;
      g_pixels = 0;
      g_cmdL = g_cmdR = 0;
      motorsCoast();
      vTaskDelay(pdMS_TO_TICKS(25));
      continue;
    }

    g_found = true;
    g_cx = cx;
    g_pixels = pixels;

    if (g_lock < LOCK_FRAMES) {
      g_cmdL = g_cmdR = 0;
      motorsCoast();
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    if (g_lock > 1000) g_lock = 1000;

    // Soft hold only when rag fills the view — otherwise full WASD-power chase.
    if (pixels > ARRIVE_BLOB_PIXELS) {
      g_cmdL = g_cmdR = 0;
      motorsCoast();
      vTaskDelay(pdMS_TO_TICKS(40));
      continue;
    }

    // Car-style: API left=A steer rack (full-throw), right=B rear drive.
    // Rack is positional: any off-centre error → full torque that way.
    const int frameCenter = CHASE_W / 2;
    float error = (float)(cx - frameCenter) / (float)frameCenter;
    int speed = DRIVE_SPEED;
    if (dist > 0 && dist < (float)CHASE_US_SLOW_CM) speed = 45;
    int steer = 0;
    // Steer sign matches UI A/D invert (rack direction).
    if (error > STEER_DEADBAND) steer = -100;      // target right of centre
    else if (error < -STEER_DEADBAND) steer = 100; // target left of centre

    g_cmdL = steer;
    g_cmdR = speed;
    driveLR(steer, speed);
    vTaskDelay(pdMS_TO_TICKS(8));
  }
}
