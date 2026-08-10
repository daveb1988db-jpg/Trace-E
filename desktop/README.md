# Trace-E Bot — Desktop Control

Kid-friendly **Spiderman Trace-E Bot** control deck: live webcam preview (~30 FPS), WASD differential steering, and on-screen Spidey-themed controls.

## Setup

From the repo root:

```bash
pip install -r requirements.txt
```

## Run

```bash
cd desktop
python trace_e_control.py
```

Or from the repo root:

```bash
python desktop/trace_e_control.py
```

## Controls

| Keys | Left Motor (LM) | Right Motor (RM) |
|------|-----------------|------------------|
| *(none)* | 0 | 0 |
| W | 100 | 100 |
| S | -100 | -100 |
| A | -100 | 100 |
| D | 100 | -100 |
| W+A | 45 | 100 |
| W+D | 100 | 45 |
| S+A | -45 | -100 |
| S+D | -100 | -45 |

Arrow keys map to WASD. Motor commands print to the console via `dispatch_hardware_commands` (hardware placeholder).

## Camera

- Default device index: **0**
- If no webcam is available, the app stays up and shows a branded placeholder frame.
