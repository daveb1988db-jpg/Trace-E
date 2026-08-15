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

// --- Pretend headlights (2× LED, resistor on each leg) ---
// Wired with the + legs on the 3.3V rail and GPIO38 as the return, so the pin
// sinks: LOW = lit, HIGH = dark. ACTIVE_LOW inverts the PWM duty so the UI's
// 0%/off really is off and 100% is full bright.
static const int PIN_HEADLIGHTS = 38;
static const bool HEADLIGHTS_ACTIVE_LOW = true;

// --- L9110S dual H-bridge (car-style chassis) ---
// Motor A = front rack-and-pinion STEER (A/D keys)
// Motor B = central rear DRIVE (W/S keys)
// API still uses ?left=&right= → left=A(steer), right=B(drive).
// Chassis has the motors crossed vs the terminal labels: the rack sits on the
// pins that used to be "rear" (42/46) and the rear motor on 21/41. Map is
// crossed here so A/D → rack and W/S → rear without rewiring.
static const int PIN_MOTOR_A_IA = 42;  // steer rack (was labelled B)
static const int PIN_MOTOR_A_IB = 46;  // steer IB — GPIO46 digital only, fine for full-throw rack
static const int PIN_MOTOR_B_IA = 21;  // rear drive (was labelled A)
static const int PIN_MOTOR_B_IB = 41;  // rear drive
static const int MOTOR_A_SCALE_PCT = 100;  // steer trim
// Fine-tune live via /api/trim if rear pull / steer strength needs it.
static const int MOTOR_A_FWD_PCT = 100;   // steer strength trim
static const int MOTOR_RIGHT_PCT = 100;   // rear drive trim


// IA HIGH is reverse on this chassis; invert so W uses IB = forward.
static const bool MOTOR_INVERT_DIR = true;
static const bool MOTOR_INVERT_B   = true;   // rear motor polarity (W was reversing)
static const bool MOTOR_INVERT_A   = false;  // steer sign handled in brain/UI (A/D)

// --- OV2640 FPC (Amazon separate module on Freenove ESP32-S3 N16R8 cam connector) ---
// Not the Freenove stock lens — same 24-pin DVP FPC pinout into the board socket.
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
