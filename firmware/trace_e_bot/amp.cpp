#include "amp.h"
#include "pins.h"

#include <WiFi.h>
#include <HTTPClient.h>
#include <string.h>
#include <math.h>
#include <esp_heap_caps.h>
#include <driver/i2s_std.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

static const int AMP_SAMPLE_RATE = 16000;
static const size_t AMP_MAX_WAV = 320 * 1024;
// Whole-song buffer in PSRAM (N16R8 has 8MB). 5MB ≈ 160s @ 16kHz mono s16.
static const size_t PLAY_URL_MAX = 5 * 1024 * 1024;
static const size_t AMP_WRITE_FRAMES = 256;

static i2s_chan_handle_t s_tx = NULL;
static bool s_i2sReady = false;
static uint8_t *g_postBuf = nullptr;
static size_t g_postLen = 0;
static size_t g_postCap = 0;
static bool g_postOverflow = false;
static bool g_playBusy = false;
static volatile bool g_playCancel = false;
static WebServer *g_ampSrv = nullptr;
static int g_ampVolume = 100;  // 0..150 — beep-kill left peanut at 0; Trace defaults loud
static QueueHandle_t g_ampQ = nullptr;

enum { AMP_JOB_WAV = 1, AMP_JOB_URL = 2, AMP_JOB_SIREN = 3 };
struct AmpJob {
  uint8_t kind;
  uint8_t *wav;
  size_t wavLen;
  char url[384];
  uint32_t ms;    // siren duration
  int loHz;       // siren low tone
  int hiHz;       // siren high tone
  uint32_t wailMs;  // one full cycle (sweep period, or hi+lo two-tone period)
  uint8_t mode;   // 0 = US wail sweep, 1 = UK hi-lo two-tone
};

static void *ampMalloc(size_t n) {
  void *p = nullptr;
  if (psramFound()) p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!p) p = malloc(n);
  return p;
}

static void ampFree(void *p) {
  if (p) free(p);
}

static bool ensureI2s(int rate) {
  if (s_i2sReady && s_tx) return true;
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  chan_cfg.auto_clear = true;
  if (i2s_new_channel(&chan_cfg, &s_tx, NULL) != ESP_OK) {
    s_tx = NULL;
    return false;
  }
  i2s_std_config_t std_cfg = {
    .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG((uint32_t)rate),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,
      .bclk = (gpio_num_t)PIN_I2S_BCLK,
      .ws = (gpio_num_t)PIN_I2S_LRC,
      .dout = (gpio_num_t)PIN_I2S_DOUT,
      .din = I2S_GPIO_UNUSED,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };
  if (i2s_channel_init_std_mode(s_tx, &std_cfg) != ESP_OK ||
      i2s_channel_enable(s_tx) != ESP_OK) {
    i2s_del_channel(s_tx);
    s_tx = NULL;
    return false;
  }
  s_i2sReady = true;
  return true;
}

static bool i2sWriteAll(const void *data, size_t bytes) {
  const uint8_t *p = (const uint8_t *)data;
  size_t left = bytes;
  while (left) {
    if (g_playCancel) return false;
    size_t written = 0;
    if (i2s_channel_write(s_tx, p, left, &written, pdMS_TO_TICKS(200)) != ESP_OK) return false;
    if (written == 0) return false;
    p += written;
    left -= written;
  }
  return true;
}

static void i2sSilence(int frames) {
  if (!s_tx || frames <= 0) return;
  static int16_t z[AMP_WRITE_FRAMES * 2];
  memset(z, 0, sizeof(z));
  while (frames > 0) {
    size_t n = (size_t)frames;
    if (n > AMP_WRITE_FRAMES) n = AMP_WRITE_FRAMES;
    size_t written = 0;
    i2s_channel_write(s_tx, z, n * 2 * sizeof(int16_t), &written, pdMS_TO_TICKS(40));
    frames -= (int)n;
  }
}

void ampStop() {
  g_playCancel = true;
  g_playBusy = false;
  if (s_tx && s_i2sReady) {
    i2sSilence(64);
    i2s_channel_disable(s_tx);
    i2s_del_channel(s_tx);
  }
  s_tx = NULL;
  s_i2sReady = false;
  Serial.println("amp: stop_audio — I2S off (silent)");
}

int ampVolumeGet() { return g_ampVolume; }

void ampVolumeSet(int v) {
  if (v < 0) v = 0;
  if (v > 150) v = 150;
  g_ampVolume = v;
}

static bool parseWav(const uint8_t *hdr, size_t len, uint16_t *channels, uint32_t *rate,
                     uint16_t *bits, uint32_t *dataOffset, uint32_t *dataBytes) {
  if (len < 44) return false;
  if (memcmp(hdr, "RIFF", 4) != 0 || memcmp(hdr + 8, "WAVE", 4) != 0) return false;
  size_t pos = 12;
  bool gotFmt = false, gotData = false;
  while (pos + 8 <= len) {
    char id[5] = {0};
    memcpy(id, hdr + pos, 4);
    uint32_t chunkSize;
    memcpy(&chunkSize, hdr + pos + 4, 4);
    pos += 8;
    if (strcmp(id, "fmt ") == 0) {
      if (pos + 16 > len) return false;
      uint16_t fmt;
      memcpy(&fmt, hdr + pos + 0, 2);
      memcpy(channels, hdr + pos + 2, 2);
      memcpy(rate, hdr + pos + 4, 4);
      memcpy(bits, hdr + pos + 14, 2);
      if (fmt != 1) return false;
      gotFmt = true;
    } else if (strcmp(id, "data") == 0) {
      *dataOffset = (uint32_t)pos;
      *dataBytes = chunkSize;
      gotData = true;
      break;
    }
    pos += chunkSize + (chunkSize & 1);
  }
  return gotFmt && gotData;
}

bool ampPlayWavBuffer(const uint8_t *wav, size_t len) {
  uint16_t channels = 0, bits = 0;
  uint32_t rate = 0, dataOff = 0, dataBytes = 0;
  if (!parseWav(wav, len, &channels, &rate, &bits, &dataOff, &dataBytes)) {
    Serial.println("amp: bad WAV");
    return false;
  }
  if (bits != 16 || (channels != 1 && channels != 2)) {
    Serial.printf("amp: unsupported ch=%u bits=%u\n", channels, bits);
    return false;
  }
  if (dataOff + dataBytes > len) dataBytes = (uint32_t)(len - dataOff);
  if (!ensureI2s((int)rate)) {
    Serial.println("amp: I2S fail");
    return false;
  }
  const int16_t *pcm = (const int16_t *)(wav + dataOff);
  const size_t frames = (dataBytes / sizeof(int16_t)) / channels;
  Serial.printf("amp play %u Hz ch=%u frames=%u vol=%d\n",
                (unsigned)rate, channels, (unsigned)frames, g_ampVolume);
  i2sSilence(48);
  int16_t out[AMP_WRITE_FRAMES * 2];
  size_t done = 0;
  while (done < frames) {
    size_t n = frames - done;
    if (n > AMP_WRITE_FRAMES) n = AMP_WRITE_FRAMES;
    for (size_t i = 0; i < n; i++) {
      int16_t l, r;
      if (channels == 1) l = r = pcm[done + i];
      else {
        l = pcm[(done + i) * 2];
        r = pcm[(done + i) * 2 + 1];
      }
      int32_t lv = ((int32_t)l * g_ampVolume) / 100;
      int32_t rv = ((int32_t)r * g_ampVolume) / 100;
      if (lv > 32767) lv = 32767;
      if (lv < -32768) lv = -32768;
      if (rv > 32767) rv = 32767;
      if (rv < -32768) rv = -32768;
      out[i * 2] = (int16_t)lv;
      out[i * 2 + 1] = (int16_t)rv;
    }
    if (g_playCancel || !i2sWriteAll(out, n * 2 * sizeof(int16_t))) return false;
    done += n;
    yield();
  }
  i2sSilence((int)(rate / 40));
  // Shut I2S down after play — undriven MAX98357A with clocks running can beep/hiss
  ampStop();
  return true;
}

// Download the WHOLE WAV into PSRAM first, then play from memory. Streaming
// during playback pinned the 2.4GHz radio for the whole song (cam froze) and
// any drive burst starved the read and killed it. One download burst instead:
// cam only hitches while buffering, then drive+cam are free during playback.
static bool playUrl(const String &url) {
  HTTPClient http;
  http.setTimeout(15000);
  http.setReuse(false);
  if (!http.begin(url)) return false;
  int code = http.GET();
  if (code != 200) {
    Serial.printf("amp play_url HTTP %d\n", code);
    http.end();
    return false;
  }
  int total = http.getSize();
  size_t cap;
  if (total > 0 && (size_t)total <= PLAY_URL_MAX) {
    cap = (size_t)total;
  } else {
    cap = PLAY_URL_MAX;  // unknown/oversized: grab up to the cap
  }
  uint8_t *buf = (uint8_t *)ampMalloc(cap);
  if (!buf) {
    Serial.printf("amp play_url: PSRAM alloc %u failed\n", (unsigned)cap);
    http.end();
    return false;
  }
  WiFiClient *stream = http.getStreamPtr();
  size_t got = 0;
  unsigned long lastData = millis();
  while (got < cap && !g_playCancel) {
    int avail = stream->available();
    if (avail > 0) {
      int r = stream->read(buf + got, min((size_t)avail, cap - got));
      if (r > 0) {
        got += (size_t)r;
        lastData = millis();
      }
      if (total > 0 && got >= (size_t)total) break;
    } else {
      if (!stream->connected() && !stream->available()) break;
      if (millis() - lastData > 15000UL) break;  // stalled feed
      delay(1);
    }
  }
  http.end();
  Serial.printf("amp play_url buffered %u/%d bytes RIFF=%d\n",
                (unsigned)got, total,
                (got >= 4 && buf[0] == 'R' && buf[1] == 'I' && buf[2] == 'F' && buf[3] == 'F'));
  if (g_playCancel || got < 44) {
    ampFree(buf);
    return false;
  }
  // Play from PSRAM — no network needed, so cam + drive keep the radio.
  bool ok = ampPlayWavBuffer(buf, got);
  ampFree(buf);
  return ok && !g_playCancel;
}

// Siren is synthesised on the ESP — no download, so it costs zero radio and
// works even if the Z400 is offline.
//   mode 0 = US "wail": continuous triangle sweep lo↔hi.
//   mode 1 = UK "hi-lo" two-tone ("nee-naw"): hi held, then lo held, sharp
//            switch. Phase stays continuous across the switch so there is no
//            click. This is the recognisable British emergency tone.
static bool ampSirenPlay(uint32_t ms, int loHz, int hiHz, uint32_t wailMs,
                         uint8_t mode) {
  const int rate = AMP_SAMPLE_RATE;
  if (!ensureI2s(rate)) return false;
  if (wailMs < 100) wailMs = 100;
  if (loHz < 100) loHz = 100;
  if (hiHz > 4000) hiHz = 4000;
  if (hiHz <= loHz) hiHz = loHz + 200;

  const uint32_t totalFrames = (uint32_t)((uint64_t)ms * (uint64_t)rate / 1000ULL);
  const uint32_t wailFrames = (uint32_t)((uint64_t)wailMs * (uint64_t)rate / 1000ULL);
  int16_t out[AMP_WRITE_FRAMES * 2];
  float phase = 0.0f;
  uint32_t done = 0;

  i2sSilence(16);
  while (done < totalFrames && !g_playCancel) {
    size_t n = AMP_WRITE_FRAMES;
    if (totalFrames - done < n) n = totalFrames - done;
    for (size_t i = 0; i < n; i++) {
      const uint32_t pos = (done + i) % wailFrames;
      float f;
      if (mode == 1) {
        // First half of the cycle = high note, second half = low note.
        f = (pos * 2 < wailFrames) ? (float)hiHz : (float)loHz;
      } else {
        float tri = (float)pos / (float)wailFrames;    // 0..1
        tri = (tri < 0.5f) ? (tri * 2.0f) : (2.0f - tri * 2.0f);
        f = (float)loHz + (float)(hiHz - loHz) * tri;
      }
      phase += 2.0f * (float)PI * f / (float)rate;
      if (phase > 2.0f * (float)PI) phase -= 2.0f * (float)PI;
      int32_t s = (int32_t)(sinf(phase) * 9000.0f);
      s = (s * g_ampVolume) / 100;
      if (s > 32767) s = 32767;
      if (s < -32768) s = -32768;
      out[i * 2] = (int16_t)s;
      out[i * 2 + 1] = (int16_t)s;
    }
    if (!i2sWriteAll(out, n * 2 * sizeof(int16_t))) break;
    done += (uint32_t)n;
    yield();
  }
  i2sSilence(rate / 40);
  ampStop();
  return true;
}

static bool ampEnqueue(const AmpJob &job) {
  if (!g_ampQ) return false;
  g_playCancel = true;  // cut current clip so UDP/cam recover fast
  AmpJob copy = job;
  if (xQueueSend(g_ampQ, &copy, pdMS_TO_TICKS(20)) == pdTRUE) return true;
  AmpJob dumped;
  if (xQueueReceive(g_ampQ, &dumped, 0) == pdTRUE) {
    if (dumped.wav) ampFree(dumped.wav);
  }
  if (xQueueSend(g_ampQ, &copy, 0) == pdTRUE) return true;
  if (copy.wav) ampFree(copy.wav);
  return false;
}

static void ampWorker(void *) {
  AmpJob job;
  for (;;) {
    if (xQueueReceive(g_ampQ, &job, portMAX_DELAY) != pdTRUE) continue;
    g_playCancel = false;
    g_playBusy = true;
    if (job.kind == AMP_JOB_WAV && job.wav) {
      ampPlayWavBuffer(job.wav, job.wavLen);
      ampFree(job.wav);
    } else if (job.kind == AMP_JOB_URL && job.url[0]) {
      playUrl(String(job.url));
    } else if (job.kind == AMP_JOB_SIREN) {
      ampSirenPlay(job.ms, job.loHz, job.hiHz, job.wailMs, job.mode);
    }
    g_playBusy = false;
  }
}

static void cors(WebServer &s) {
  s.sendHeader("Access-Control-Allow-Origin", "*");
  s.sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  s.sendHeader("Access-Control-Allow-Headers", "*");
  s.sendHeader("Cache-Control", "no-store");
}

static void handlePlayUrl() {
  WebServer &s = *g_ampSrv;
  cors(s);
  String url;
  if (s.hasArg("url")) url = s.arg("url");
  if (url.length() < 8) {
    s.send(400, "application/json", "{\"ok\":false,\"error\":\"url required\",\"played\":false}");
    return;
  }
  // ACK immediately — I2S play runs on ampWorker so drive UDP on this
  // server keeps ticking. Play-then-ACK froze WASD for the whole clip.
  AmpJob job;
  memset(&job, 0, sizeof(job));
  job.kind = AMP_JOB_URL;
  url.toCharArray(job.url, sizeof(job.url));
  if (!ampEnqueue(job)) {
    s.send(503, "application/json", "{\"ok\":false,\"error\":\"amp queue full\",\"played\":false}");
    return;
  }
  s.send(200, "application/json", "{\"ok\":true,\"playing\":true,\"queued\":true,\"via\":\"play_url\"}");
}

static bool postBufEnsure(size_t need) {
  if (need > AMP_MAX_WAV) {
    g_postOverflow = true;
    return false;
  }
  if (g_postBuf && g_postCap >= need) return true;
  size_t cap = g_postCap ? g_postCap : 4096;
  while (cap < need) {
    size_t next = cap * 2;
    if (next < cap || next > AMP_MAX_WAV) {
      cap = AMP_MAX_WAV;
      break;
    }
    cap = next;
  }
  if (cap < need) cap = need;
  uint8_t *nb = (uint8_t *)ampMalloc(cap);
  if (!nb) {
    g_postOverflow = true;
    Serial.printf("play_wav realloc fail need=%u\n", (unsigned)need);
    return false;
  }
  if (g_postBuf && g_postLen) memcpy(nb, g_postBuf, g_postLen);
  if (g_postBuf) ampFree(g_postBuf);
  g_postBuf = nb;
  g_postCap = cap;
  return true;
}

static void handlePlayWavBody() {
  WebServer &s = *g_ampSrv;
  String ct = s.header("Content-Type");
  ct.toLowerCase();
  const bool multipart = ct.startsWith("multipart/");

  if (multipart) {
    HTTPUpload &upload = s.upload();
    if (upload.status == UPLOAD_FILE_START) {
      if (g_postBuf) {
        ampFree(g_postBuf);
        g_postBuf = nullptr;
      }
      g_postLen = g_postCap = 0;
      g_postOverflow = false;
      Serial.printf("play_wav multipart start name=%s\n", upload.filename.c_str());
    } else if (upload.status == UPLOAD_FILE_WRITE) {
      if (g_postOverflow) return;
      size_t need = g_postLen + upload.currentSize;
      if (!postBufEnsure(need)) return;
      memcpy(g_postBuf + g_postLen, upload.buf, upload.currentSize);
      g_postLen += upload.currentSize;
    } else if (upload.status == UPLOAD_FILE_END) {
      Serial.printf("play_wav multipart end: %u bytes overflow=%d\n",
                    (unsigned)g_postLen, (int)g_postOverflow);
    } else if (upload.status == UPLOAD_FILE_ABORTED) {
      g_postOverflow = true;
    }
    return;
  }

  // Raw audio/wav (or octet-stream) body — requires collectHeaders(Content-Length)
  HTTPRaw &raw = s.raw();
  if (raw.status == RAW_START) {
    if (g_postBuf) {
      ampFree(g_postBuf);
      g_postBuf = nullptr;
    }
    g_postLen = 0;
    g_postCap = 0;
    g_postOverflow = false;
    size_t need = 0;
    if (s.hasHeader("Content-Length")) need = (size_t)s.header("Content-Length").toInt();
    if (need < 44 || need > AMP_MAX_WAV) {
      // Unknown size — grow dynamically from a small seed (camera leaves little DRAM)
      need = 8 * 1024;
      Serial.printf("play_wav raw Content-Length missing/bad — seed %u\n", (unsigned)need);
    }
    if (!postBufEnsure(need)) {
      Serial.println("play_wav: alloc failed at START");
      return;
    }
    Serial.printf("play_wav raw start need=%u cap=%u\n", (unsigned)need, (unsigned)g_postCap);
  } else if (raw.status == RAW_WRITE) {
    if (g_postOverflow || !g_postBuf) return;
    size_t need = g_postLen + raw.currentSize;
    if (!postBufEnsure(need)) return;
    memcpy(g_postBuf + g_postLen, raw.buf, raw.currentSize);
    g_postLen += raw.currentSize;
  } else if (raw.status == RAW_END) {
    Serial.printf("play_wav raw end: %u bytes overflow=%d\n",
                  (unsigned)g_postLen, (int)g_postOverflow);
  } else if (raw.status == RAW_ABORTED) {
    g_postOverflow = true;
    Serial.println("play_wav raw aborted");
  }
}

static void handlePlayWav() {
  WebServer &s = *g_ampSrv;
  cors(s);
  if (g_postOverflow || !g_postBuf || g_postLen < 44) {
    Serial.printf("play_wav reject: buf=%p len=%u overflow=%d\n",
                  (void *)g_postBuf, (unsigned)g_postLen, (int)g_postOverflow);
    s.send(400, "application/json",
           "{\"ok\":false,\"error\":\"bad wav body\",\"played\":false}");
    if (g_postBuf) {
      ampFree(g_postBuf);
      g_postBuf = nullptr;
    }
    g_postLen = g_postCap = 0;
    g_postOverflow = false;
    return;
  }
  uint8_t *buf = g_postBuf;
  size_t len = g_postLen;
  g_postBuf = nullptr;
  g_postLen = g_postCap = 0;
  g_postOverflow = false;
  AmpJob job;
  memset(&job, 0, sizeof(job));
  job.kind = AMP_JOB_WAV;
  job.wav = buf;
  job.wavLen = len;
  if (!ampEnqueue(job)) {
    s.send(503, "application/json",
           "{\"ok\":false,\"error\":\"amp queue full\",\"played\":false}");
    return;
  }
  s.send(200, "application/json", "{\"ok\":true,\"playing\":true,\"queued\":true}");
}

static void handleAmpOptions() {
  cors(*g_ampSrv);
  g_ampSrv->send(204);
}

static void handleSiren() {
  WebServer &s = *g_ampSrv;
  cors(s);
  AmpJob job;
  memset(&job, 0, sizeof(job));
  job.kind = AMP_JOB_SIREN;
  job.ms = s.hasArg("ms") ? (uint32_t)s.arg("ms").toInt() : 3000;
  // Default = UK "hi-lo" two-tone. mode=wail (or mode=0) gives the US sweep.
  String md = s.hasArg("mode") ? s.arg("mode") : String("uk");
  md.toLowerCase();
  job.mode = (md == "wail" || md == "sweep" || md == "us" || md == "0") ? 0 : 1;
  if (job.mode == 1) {
    // Classic British two-tone notes: ~970 Hz over ~600 Hz, ~1s per full
    // nee-naw (≈500 ms each note).
    job.loHz = s.hasArg("lo") ? s.arg("lo").toInt() : 600;
    job.hiHz = s.hasArg("hi") ? s.arg("hi").toInt() : 970;
    job.wailMs = s.hasArg("wail") ? (uint32_t)s.arg("wail").toInt() : 1000;
  } else {
    job.loHz = s.hasArg("lo") ? s.arg("lo").toInt() : 600;
    job.hiHz = s.hasArg("hi") ? s.arg("hi").toInt() : 1600;
    job.wailMs = s.hasArg("wail") ? (uint32_t)s.arg("wail").toInt() : 700;
  }
  if (job.ms < 200) job.ms = 200;
  if (job.ms > 15000) job.ms = 15000;  // never let a stuck request wail forever
  if (!ampEnqueue(job)) {
    s.send(503, "application/json", "{\"ok\":false,\"error\":\"amp queue full\"}");
    return;
  }
  char buf[192];
  snprintf(buf, sizeof(buf),
           "{\"ok\":true,\"siren\":true,\"mode\":%u,\"ms\":%u,\"lo\":%d,\"hi\":%d,\"wail\":%u}",
           (unsigned)job.mode, (unsigned)job.ms, job.loHz, job.hiHz,
           (unsigned)job.wailMs);
  s.send(200, "application/json", buf);
}

static void handleStopAudio() {
  cors(*g_ampSrv);
  g_playCancel = true;
  if (!g_playBusy) ampStop();
  g_ampSrv->send(200, "application/json", "{\"ok\":true,\"stopped\":true,\"i2s\":\"stopping\"}");
}

static void handleVolume() {
  WebServer &s = *g_ampSrv;
  cors(s);
  int v = g_ampVolume;
  if (s.hasArg("level")) v = s.arg("level").toInt();
  else if (s.hasArg("v")) v = s.arg("v").toInt();
  else if (s.hasArg("pct")) v = s.arg("pct").toInt();
  else if (s.hasArg("volume")) v = s.arg("volume").toInt();
  ampVolumeSet(v);
  char buf[96];
  snprintf(buf, sizeof(buf), "{\"ok\":true,\"volume\":%d}", g_ampVolume);
  s.send(200, "application/json", buf);
  Serial.printf("amp volume=%d\n", g_ampVolume);
}

void ampInit() {
  // Lazy I2S on first play — keep boot quiet (no chirp / boot tone)
  g_ampVolume = 100;
  g_ampQ = xQueueCreate(2, sizeof(AmpJob));
  if (g_ampQ) {
    // Core 0 (with cam), NOT core 1 — drive task (prio 5) on core 1 was
    // starving audio and cutting songs when driving.
    xTaskCreatePinnedToCore(ampWorker, "ampPlay", 8192, nullptr, 3, nullptr, 0);
  }
  Serial.printf("Amp MAX98357A pins BCLK=%d LRC=%d DOUT=%d (lazy I2S, vol=%d, silent boot, async play)\n",
                PIN_I2S_BCLK, PIN_I2S_LRC, PIN_I2S_DOUT, g_ampVolume);
}

void ampRegisterRoutes(WebServer &s) {
  g_ampSrv = &s;
  // Critical: without this, raw() never sees Content-Length → empty body → HTTP 400
  static const char *headerKeys[] = {
      "Content-Type", "Content-Length", "Content-Disposition", "Accept", "User-Agent"};
  s.collectHeaders(headerKeys, 5);

  s.on("/api/play_url", HTTP_GET, handlePlayUrl);
  s.on("/api/play_url", HTTP_POST, handlePlayUrl);
  s.on("/api/play_url", HTTP_OPTIONS, handleAmpOptions);
  s.on("/api/play_wav", HTTP_POST, handlePlayWav, handlePlayWavBody);
  s.on("/api/play_wav", HTTP_OPTIONS, handleAmpOptions);
  s.on("/api/siren", HTTP_GET, handleSiren);
  s.on("/api/siren", HTTP_POST, handleSiren);
  s.on("/api/siren", HTTP_OPTIONS, handleAmpOptions);
  s.on("/api/stop_audio", HTTP_GET, handleStopAudio);
  s.on("/api/stop_audio", HTTP_POST, handleStopAudio);
  s.on("/api/stop_audio", HTTP_OPTIONS, handleAmpOptions);
  s.on("/api/volume", HTTP_GET, handleVolume);
  s.on("/api/volume", HTTP_POST, handleVolume);
  s.on("/api/volume", HTTP_OPTIONS, handleAmpOptions);
  Serial.println("Amp routes: /api/play_wav /api/play_url /api/siren /api/stop_audio /api/volume on this server");
}
