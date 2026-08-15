#include "motors.h"
#include "driver/gpio.h"

static int g_lastPhysA = 0;
static int g_lastPhysB = 0;
static int g_trimLeftFwd = MOTOR_A_FWD_PCT;
static int g_trimRight = MOTOR_RIGHT_PCT;
static MotorsRangeFn g_rangeFn = nullptr;
static float g_bumperStopCm = 10.0f;
static volatile bool g_bumperBlocked = false;
static bool g_bumperEnabled = true;

static int clampSigned(int v) {
  if (v > 100) return 100;
  if (v < -100) return -100;
  return v;
}

static int clampTrim(int v) {
  if (v < 50) return 50;
  if (v > 120) return 120;
  return v;
}

static int applyInvert(int signedVal, bool motorB) {
  if (MOTOR_INVERT_DIR) signedVal = -signedVal;
  if (motorB && MOTOR_INVERT_B) signedVal = -signedVal;
  if (!motorB && MOTOR_INVERT_A) signedVal = -signedVal;  // steer rack
  return signedVal;
}

static void gpioOut(int pin) {
  pinMode(pin, OUTPUT);
  if (pin == 46) {
    gpio_pulldown_dis(GPIO_NUM_46);
    gpio_pullup_dis(GPIO_NUM_46);
    gpio_set_drive_capability(GPIO_NUM_46, GPIO_DRIVE_CAP_3);
  } else {
    gpio_set_drive_capability((gpio_num_t)pin, GPIO_DRIVE_CAP_3);
    gpio_pullup_dis((gpio_num_t)pin);
    gpio_pulldown_dis((gpio_num_t)pin);
  }
}

static void digi(int pin, int level) {
  digitalWrite(pin, level ? HIGH : LOW);
}

void motorsTrimGet(int *leftFwdPct, int *rightPct) {
  if (leftFwdPct) *leftFwdPct = g_trimLeftFwd;
  if (rightPct) *rightPct = g_trimRight;
}

void motorsTrimSet(int leftFwdPct, int rightPct) {
  g_trimLeftFwd = clampTrim(leftFwdPct);
  g_trimRight = clampTrim(rightPct);
  // Force next setMotor to re-apply PWM with new trim.
  g_lastPhysA = 0x7fff;
  g_lastPhysB = 0x7fff;
}

void motorsTrimNudge(int side, int delta) {
  if (delta < -20) delta = -20;
  if (delta > 20) delta = 20;
  if (side == 0) motorsTrimSet(g_trimLeftFwd + delta, g_trimRight);
  else motorsTrimSet(g_trimLeftFwd, g_trimRight + delta);
}

void motorsSetBumper(MotorsRangeFn rangeFn, float stopCm) {
  g_rangeFn = rangeFn;
  if (stopCm < 5.0f) stopCm = 5.0f;
  if (stopCm > 80.0f) stopCm = 80.0f;
  g_bumperStopCm = stopCm;
}

bool motorsBumperBlocked() { return g_bumperBlocked; }

void motorsSetBumperEnabled(bool on) {
  g_bumperEnabled = on;
  if (!on) g_bumperBlocked = false;
}

bool motorsBumperEnabled() { return g_bumperEnabled; }

void motorsCoast() {
  ledcWrite(PIN_MOTOR_A_IA, 0);
  digitalWrite(PIN_MOTOR_A_IA, LOW);
  digi(PIN_MOTOR_A_IB, 0);
  ledcWrite(PIN_MOTOR_B_IA, 0);
  digi(PIN_MOTOR_B_IB, 0);
  g_lastPhysA = 0;
  g_lastPhysB = 0;
}

void motorsInit() {
  g_trimLeftFwd = clampTrim(MOTOR_A_FWD_PCT);
  g_trimRight = clampTrim(MOTOR_RIGHT_PCT);
  const int dead[] = {38, 39, 40};
  for (int i = 0; i < 3; i++) {
    gpio_reset_pin((gpio_num_t)dead[i]);
    gpioOut(dead[i]);
    digi(dead[i], 0);
  }
  pinMode(19, INPUT);
  gpioOut(PIN_MOTOR_A_IA);
  gpioOut(PIN_MOTOR_A_IB);
  gpioOut(PIN_MOTOR_B_IA);
  gpioOut(PIN_MOTOR_B_IB);
  // PWM only on IA legs — GPIO46 (B-IB) will not PWM. IB legs are digital.
  // 8 kHz: L9110S switches poorly at 20 kHz (lost torque at part throttle).
  ledcAttachChannel(PIN_MOTOR_A_IA, 8000, 8, 4);
  ledcAttachChannel(PIN_MOTOR_B_IA, 8000, 8, 6);
  motorsCoast();
  Serial.printf("L9110S A=%d/%d B=%d/%d trim Lfwd=%d R=%d\n",
                PIN_MOTOR_A_IA, PIN_MOTOR_A_IB, PIN_MOTOR_B_IA, PIN_MOTOR_B_IB,
                g_trimLeftFwd, g_trimRight);
}

static uint8_t dutyOf(int phys, bool motorB) {
  int mag = abs(phys);
  if (motorB) {
    if (g_trimRight != 100) {
      mag = (mag * g_trimRight + 50) / 100;
    }
  } else {
    const int scale = (phys < 0) ? g_trimLeftFwd : MOTOR_A_SCALE_PCT;
    if (scale != 100) {
      mag = (mag * scale + 50) / 100;
    }
  }
  if (mag <= 0) return 0;
  if (mag >= 100) return 255;
  return (uint8_t)((mag * 255 + 50) / 100);
}

static void setLeg(int iaPin, int ibPin, int phys, bool motorB) {
  if (phys == 0) {
    ledcWrite(iaPin, 0);
    if (!motorB) digitalWrite(iaPin, LOW);
    digi(ibPin, 0);
    return;
  }
  // Steer: slam digital rails for hard A/D; PWM only for the weak snap-back
  // burst (GPIO42 can PWM, GPIO46 IB cannot). LEDC 255 was too weak for lock.
  if (!motorB) {
    if (abs(phys) >= 80) {
      ledcWrite(iaPin, 0);
      pinMode(iaPin, OUTPUT);
      pinMode(ibPin, OUTPUT);
      if (phys > 0) {
        digitalWrite(iaPin, HIGH);
        digitalWrite(ibPin, LOW);
      } else {
        digitalWrite(iaPin, LOW);
        digitalWrite(ibPin, HIGH);
      }
      return;
    }
    ledcAttachChannel(iaPin, 8000, 8, 4);
    const uint8_t duty = dutyOf(phys, false);
    if (phys > 0) {
      digi(ibPin, 0);
      ledcWrite(iaPin, duty);
    } else {
      digi(ibPin, 1);
      ledcWrite(iaPin, (uint8_t)(255 - duty));
    }
    return;
  }
  const uint8_t duty = dutyOf(phys, motorB);
  if (phys > 0) {
    digi(ibPin, 0);
    ledcWrite(iaPin, duty);
  } else {
    digi(ibPin, 1);
    ledcWrite(iaPin, (uint8_t)(255 - duty));
  }
}

void setMotor(MotorSide side, int speedSigned) {
  const bool motorB = (side == MOTOR_RIGHT);
  const int iaPin = motorB ? PIN_MOTOR_B_IA : PIN_MOTOR_A_IA;
  const int ibPin = motorB ? PIN_MOTOR_B_IB : PIN_MOTOR_A_IB;
  int *lastPhys = motorB ? &g_lastPhysB : &g_lastPhysA;
  int phys = clampSigned(applyInvert(clampSigned(speedSigned), motorB));
  if (phys == *lastPhys) return;
  setLeg(iaPin, ibPin, phys, motorB);
  *lastPhys = phys;
}

void driveLR(int leftSigned, int rightSigned) {
  leftSigned = clampSigned(leftSigned);
  rightSigned = clampSigned(rightSigned);
  if (leftSigned == 0 && rightSigned == 0) {
    g_bumperBlocked = false;
    motorsCoast();
    return;
  }

  // Hard bumper: block REAR forward only (API right = channel B).
  // Steering (left/A) must still work at the bumper so we can turn away.
  // Fail-OPEN on no echo — fail-closed was killing W whenever SR04 glitched.
  if (g_bumperEnabled && g_rangeFn && rightSigned > 8) {
    float cm = g_rangeFn();
    if (cm > 0.0f && cm < g_bumperStopCm) {
      g_bumperBlocked = true;
      setMotor(MOTOR_LEFT, leftSigned);  // keep rack alive
      setMotor(MOTOR_RIGHT, 0);
      return;
    }
  }
  g_bumperBlocked = false;

  setMotor(MOTOR_LEFT, leftSigned);   // A = steer
  setMotor(MOTOR_RIGHT, rightSigned); // B = drive
}
