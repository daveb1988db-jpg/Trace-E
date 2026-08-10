#pragma once
/*
 * Trace-E Bot — L9110S differential drive skeleton.
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
