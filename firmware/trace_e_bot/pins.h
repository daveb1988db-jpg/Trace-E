#pragma once
/*
 * Trace-E Bot — locked pin map (ESP32-S3 Freenove OV2640 FPC lab chassis).
 * Same physical wiring as the peanut-robot ESP32-S3 Freenove lab.
 */

// --- Amp (MAX98357A I2S) ---
static const int PIN_I2S_BCLK = 48;
static const int PIN_I2S_LRC  = 47;
static const int PIN_I2S_DOUT = 14;

// --- Mic (GY-MAX4466 / MAX4466) ---
static const int PIN_MIC = 3;

// --- Ultrasonic (HC-SR04) — front bumper proximity ---
// TRIG → GPIO1, ECHO → GPIO2 (5V VCC / GND shared; ECHO via divider if needed)
static const int PIN_TRIG = 1;
static const int PIN_ECHO = 2;

// --- L9110S dual H-bridge (differential drive) ---
// Motor A = left wheel, Motor B = right wheel
static const int PIN_MOTOR_A_IA = 21;  // A-1A left
static const int PIN_MOTOR_A_IB = 41;  // A-1B left
static const int PIN_MOTOR_B_IA = 42;  // B-1A right
static const int PIN_MOTOR_B_IB = 46;  // B-1B right

// Chassis polarity (tune on first drive test)
static const bool MOTOR_INVERT_DIR = true;   // flip both wheels if "forward" is reverse
static const bool MOTOR_INVERT_B   = false;  // flip right only if wheels fight

// LEDC: keep ch0/timer0 free for camera XCLK later; motors use ch4–7
static const int MOTOR_PWM_FREQ_HZ = 500;
static const int MOTOR_PWM_RES_BITS = 8;
static const int MOTOR_LEDC_CH_A_IA = 4;
static const int MOTOR_LEDC_CH_A_IB = 5;
static const int MOTOR_LEDC_CH_B_IA = 6;
static const int MOTOR_LEDC_CH_B_IB = 7;

// --- OV2640 FPC (Freenove ESP32-S3 WROOM / N16R8) ---
static const int PIN_CAM_PWDN  = -1;
static const int PIN_CAM_RESET = -1;
static const int PIN_CAM_XCLK  = 15;
static const int PIN_CAM_SIOD  = 4;
static const int PIN_CAM_SIOC  = 5;
static const int PIN_CAM_Y2    = 11;
static const int PIN_CAM_Y3    = 9;
static const int PIN_CAM_Y4    = 8;
static const int PIN_CAM_Y5    = 10;
static const int PIN_CAM_Y6    = 12;
static const int PIN_CAM_Y7    = 18;
static const int PIN_CAM_Y8    = 17;
static const int PIN_CAM_Y9    = 16;
static const int PIN_CAM_VSYNC = 6;
static const int PIN_CAM_HREF  = 7;
static const int PIN_CAM_PCLK  = 13;
