#pragma once
/*
 * Trace-E Bot — L9110S car-style drive.
 * Channel A (MOTOR_LEFT / API left)  = front rack STEER
 * Channel B (MOTOR_RIGHT / API right) = rear DRIVE
 * Safe rule: never drive IA and IB HIGH together (H-bridge shoot-through).
 * Coast = both legs LOW. Forward = IA PWM / IB LOW. Reverse = IA LOW / IB PWM.
 */

#include <Arduino.h>
#include "pins.h"

enum MotorSide : int {
  MOTOR_LEFT = 0,
  MOTOR_RIGHT = 1,
};

void motorsInit();
void motorsCoast();
/** Signed speed −100..+100. 0 = coast. Never asserts IA+IB HIGH. */
void setMotor(MotorSide side, int speedSigned);
/** Apply left/right signed speeds (−100..+100). */
void driveLR(int leftSigned, int rightSigned);

/** Runtime straight-drive trim (percent). Applies to WASD + chase. */
void motorsTrimGet(int *leftFwdPct, int *rightPct);
void motorsTrimSet(int leftFwdPct, int rightPct);
/** Nudge one side by delta (−20..+20). side: 0=left forward, 1=right. */
void motorsTrimNudge(int side, int delta);

/** Optional front bumper: range fn returns cm, or <0 if no echo. */
typedef float (*MotorsRangeFn)();
void motorsSetBumper(MotorsRangeFn rangeFn, float stopCm);
/** True if last driveLR was blocked by bumper. */
bool motorsBumperBlocked();
/** Enable/disable the front bumper stop at runtime. */
void motorsSetBumperEnabled(bool on);
bool motorsBumperEnabled();
