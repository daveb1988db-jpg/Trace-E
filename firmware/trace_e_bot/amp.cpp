#include "amp.h"
#include "pins.h"

#include <WiFi.h>
#include <HTTPClient.h>
#include <string.h>
#include <math.h>
#include <esp_heap_caps.h>
#include <driver/i2s_std.h>

static const int AMP_SAMPLE_RATE = 16000;
static const size_t AMP_MAX_WAV = 320 * 1024;
static const size_t AMP_WRITE_FRAMES = 256;

static i2s_chan_handle_t s_tx = NULL;
static bool s_i2sReady = false;
static uint8_t *g_postBuf = nullptr;
static size_t g_postLen = 0;
static size_t g_postCap = 0;
static bool g_postOverflow = false;
static bool g_playBusy = false;
static WebServer *g_ampSrv = nullptr;
static int g_ampVolume = 100;  // 0..150 — beep-kill left peanut at 0; Trace defaults loud

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
    if (!i2sWriteAll(out, n * 2 * sizeof(int16_t))) return false;
    done += n;
    yield();
  }
  i2sSilence((int)(rate / 40));
  // Shut I2S down after play — undriven MAX98357A with clocks running can beep/hiss
  ampStop();
  return true;
}

static bool playUrl(const String &url) {
  HTTPClient http;
  http.setTimeout(45000);
  if (!http.begin(url)) return false;
  int code = http.GET();
  if (code != 200) {
    Serial.printf("amp play_url HTTP %d\n", code);
    http.end();
    return false;
  }
  int total = http.getSize();
  WiFiClient *stream = http.getStreamPtr();
  size_t cap = (total > 0 && (size_t)total <= AMP_MAX_WAV) ? (size_t)total : AMP_MAX_WAV;
  uint8_t *buf = (uint8_t *)ampMalloc(cap);
  if (!buf) {
    http.end();
    return false;
  }
  size_t got = 0;
  unsigned long t0 = millis();
  while (got < cap && millis() - t0 < 45000UL) {
    size_t avail = stream->available();
    if (avail) {
      got += stream->readBytes(buf + got, min(avail, cap - got));
      if (total > 0 && got >= (size_t)total) break;
    } else {
      if (!stream->connected() && !stream->available()) break;
      delay(1);
    }
  }
  http.end();
  Serial.printf("amp play_url got=%u RIFF=%d\n", (unsigned)got,
                (got >= 4 && buf[0] == 'R' && buf[1] == 'I' && buf[2] == 'F' && buf[3] == 'F'));
  g_playBusy = true;
  bool ok = ampPlayWavBuffer(buf, got);
  g_playBusy = false;
  ampFree(buf);
  return ok;
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
  // Play FIRST, then ACK — avoids false 200 while amp never spoke
  g_playBusy = true;
  bool ok = playUrl(url);
  g_playBusy = false;
  if (ok) {
    s.send(200, "application/json", "{\"ok\":true,\"played\":true,\"via\":\"play_url\"}");
  } else {
    s.send(502, "application/json", "{\"ok\":false,\"error\":\"play_url failed\",\"played\":false}");
  }
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
  // Play then ACK so client knows amp actually spoke
  g_playBusy = true;
  bool ok = ampPlayWavBuffer(buf, len);
  g_playBusy = false;
  ampFree(buf);
  if (ok) {
    s.send(200, "application/json", "{\"ok\":true,\"playing\":true,\"played\":true}");
  } else {
    s.send(500, "application/json", "{\"ok\":false,\"error\":\"amp play failed\",\"played\":false}");
  }
}

static void handleAmpOptions() {
  cors(*g_ampSrv);
  g_ampSrv->send(204);
}

static void handleStopAudio() {
  cors(*g_ampSrv);
  ampStop();
  g_ampSrv->send(200, "application/json", "{\"ok\":true,\"stopped\":true,\"i2s\":false}");
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
  Serial.printf("Amp MAX98357A pins BCLK=%d LRC=%d DOUT=%d (lazy I2S, vol=%d, silent boot)\n",
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
  s.on("/api/stop_audio", HTTP_GET, handleStopAudio);
  s.on("/api/stop_audio", HTTP_POST, handleStopAudio);
  s.on("/api/stop_audio", HTTP_OPTIONS, handleAmpOptions);
  s.on("/api/volume", HTTP_GET, handleVolume);
  s.on("/api/volume", HTTP_POST, handleVolume);
  s.on("/api/volume", HTTP_OPTIONS, handleAmpOptions);
  Serial.println("Amp routes: /api/play_wav /api/play_url /api/stop_audio /api/volume on this server");
}
