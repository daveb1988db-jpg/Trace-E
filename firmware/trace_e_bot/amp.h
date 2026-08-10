#pragma once
/*
 * Trace-E Bot — MAX98357A I2S amp (play PCM WAV on :8765).
 * No boot / drive / idle tones — only explicit /api/play_wav and /api/play_url.
 * /api/stop_audio shuts I2S down so the amp stays silent.
 */

#include <Arduino.h>
#include <WebServer.h>

void ampInit();
/** Register /api/play_wav + /api/play_url + /api/stop_audio + /api/volume on the given server (use drive :8765). */
void ampRegisterRoutes(WebServer &s);
bool ampPlayWavBuffer(const uint8_t *wav, size_t len);
/** Abort play, write silence, disable I2S TX (kills hiss/beep from undriven amp). */
void ampStop();
int ampVolumeGet();
void ampVolumeSet(int v);
