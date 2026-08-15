#pragma once
/*
 * On-ESP bright-orange chase — local camera→motors (no Wi-Fi in the drive path).
 */

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

#ifndef CHASE_STOP_CM
#define CHASE_STOP_CM 15
#endif

void chaseInit();
/** Enable/disable. Re-inits camera (RGB565 QVGA on, JPEG VGA off). */
bool chaseSetEnabled(bool on);
bool chaseEnabled();
void chaseStatus(bool *found, int *cx, int *pixels, float *usCm);
int chaseCmdLeft();
int chaseCmdRight();
/** Malloc'd JPEG preview copy while chase is publishing. Caller frees. */
bool chaseCloneJpeg(uint8_t **out, size_t *outLen);
/** Core-1 task entry (created once from setup). */
void chaseTask(void *pv);

/** Called from firmware when leaving chase so JPEG FPV can be restored. */
typedef bool (*ChaseCamRestoreFn)();
void chaseSetRestoreFn(ChaseCamRestoreFn fn);
