#include "motors.h"

static int g_lastPhysA = 0;
static int g_lastPhysB = 0;
static bool g_pwmReady = false;

static int clampSigned(int v) {
  if (v > 100) return 100;
  if (v < -100) return -100;
  return v;
}

static int applyInvert(int signedVal, bool motorB) {
  if (MOTOR_INVERT_DIR) signedVal = -signedVal;
  if (motorB && MOTOR_INVERT_B) signedVal = -signedVal;
  return signedVal;
}

static uint8_t pctToDuty(int pct) {
  if (pct <= 0) return 0;
  if (pct >= 100) return 255;
  return (uint8_t)((pct * 255 + 50) / 100);
}

static void detachToGpioLow(int iaPin, int ibPin) {
  ledcDetach(iaPin);
  ledcDetach(ibPin);
  pinMode(iaPin, OUTPUT);
  pinMode(ibPin, OUTPUT);
  digitalWrite(iaPin, LOW);
  digitalWrite(ibPin, LOW);
}

static bool attachPwmLeg(int iaPin, int ibPin, int chIa, int chIb) {
  bool ok = true;
  if (!ledcAttachChannel(iaPin, MOTOR_PWM_FREQ_HZ, MOTOR_PWM_RES_BITS, chIa)) ok = false;
  if (!ledcAttachChannel(ibPin, MOTOR_PWM_FREQ_HZ, MOTOR_PWM_RES_BITS, chIb)) ok = false;
  if (ok) {
    ledcWrite(iaPin, 0);
    ledcWrite(ibPin, 0);
    g_pwmReady = true;
  }
  return ok;
}

void motorsCoast() {
  detachToGpioLow(PIN_MOTOR_A_IA, PIN_MOTOR_A_IB);
  detachToGpioLow(PIN_MOTOR_B_IA, PIN_MOTOR_B_IB);
  g_pwmReady = false;
  g_lastPhysA = 0;
  g_lastPhysB = 0;
}

void motorsInit() {
  pinMode(PIN_MOTOR_A_IA, OUTPUT);
  pinMode(PIN_MOTOR_A_IB, OUTPUT);
  pinMode(PIN_MOTOR_B_IA, OUTPUT);
  pinMode(PIN_MOTOR_B_IB, OUTPUT);
  motorsCoast();
  Serial.printf(
      "L9110S ready A=%d/%d B=%d/%d invert_dir=%s invert_b=%s (coast-safe)\n",
      PIN_MOTOR_A_IA, PIN_MOTOR_A_IB, PIN_MOTOR_B_IA, PIN_MOTOR_B_IB,
      MOTOR_INVERT_DIR ? "true" : "false",
      MOTOR_INVERT_B ? "true" : "false");
}

void setMotor(MotorSide side, int speedSigned) {
  const bool motorB = (side == MOTOR_RIGHT);
  const int iaPin = motorB ? PIN_MOTOR_B_IA : PIN_MOTOR_A_IA;
  const int ibPin = motorB ? PIN_MOTOR_B_IB : PIN_MOTOR_A_IB;
  int *lastPhys = motorB ? &g_lastPhysB : &g_lastPhysA;

  speedSigned = clampSigned(speedSigned);
  int phys = clampSigned(applyInvert(speedSigned, motorB));

  // Direction change: coast first (never fight through shoot-through risk)
  if (*lastPhys != 0 && phys != 0 &&
      ((*lastPhys > 0 && phys < 0) || (*lastPhys < 0 && phys > 0))) {
    detachToGpioLow(iaPin, ibPin);
    g_pwmReady = false;
    *lastPhys = 0;
  }

  if (phys == 0) {
    detachToGpioLow(iaPin, ibPin);
    g_pwmReady = false;
    *lastPhys = 0;
    return;
  }

  // Full rail: digitalWrite one HIGH, other LOW — never both HIGH
  if (abs(phys) >= 100) {
    detachToGpioLow(iaPin, ibPin);
    g_pwmReady = false;
    if (phys > 0) {
      digitalWrite(iaPin, HIGH);
      digitalWrite(ibPin, LOW);
    } else {
      digitalWrite(iaPin, LOW);
      digitalWrite(ibPin, HIGH);
    }
    *lastPhys = phys;
    return;
  }

  const int chIa = motorB ? MOTOR_LEDC_CH_B_IA : MOTOR_LEDC_CH_A_IA;
  const int chIb = motorB ? MOTOR_LEDC_CH_B_IB : MOTOR_LEDC_CH_A_IB;
  if (!attachPwmLeg(iaPin, ibPin, chIa, chIb)) {
    // Fallback digital (still never both HIGH)
    pinMode(iaPin, OUTPUT);
    pinMode(ibPin, OUTPUT);
    digitalWrite(iaPin, LOW);
    digitalWrite(ibPin, LOW);
    if (phys > 0) digitalWrite(iaPin, HIGH);
    else digitalWrite(ibPin, HIGH);
    *lastPhys = phys;
    return;
  }

  // Explicit zero both legs before asserting one PWM leg
  ledcWrite(iaPin, 0);
  ledcWrite(ibPin, 0);
  const uint8_t duty = pctToDuty(abs(phys));
  if (phys > 0) {
    ledcWrite(iaPin, duty);
    ledcWrite(ibPin, 0);
  } else {
    ledcWrite(iaPin, 0);
    ledcWrite(ibPin, duty);
  }
  *lastPhys = phys;
  (void)g_pwmReady;
}

void driveLR(int leftSigned, int rightSigned) {
  leftSigned = clampSigned(leftSigned);
  rightSigned = clampSigned(rightSigned);
  if (leftSigned == 0 && rightSigned == 0) {
    motorsCoast();
    return;
  }
  setMotor(MOTOR_LEFT, leftSigned);
  setMotor(MOTOR_RIGHT, rightSigned);
}
