# Trace-E ROS2 person follow (Docker)

Ubuntu 26.04 on this Z400 has no official ROS 2 apt distro yet. Run **ROS 2 Jazzy** in Docker and reuse the same `desktop/follow_person.py` brain math (fast forward + slow differential turn).

## Quick start (WSL)

```bash
cd /mnt/c/Users/Bartl/Projects/Trace-E/ros2_ws
docker compose up --build
```

Env:
- `TRACE_E_ESP_BASE=http://192.168.1.104`
- Host network so the container reaches the ESP on LAN Wi‑Fi

## Nodes

| Node | Role |
|------|------|
| `trace_e_follow` | MJPEG in → HOG person detect → `/api/drive` out |

Day-to-day testing still uses the HQ UI **Follow ON** button (speak_server embeds the same controller without Docker).
