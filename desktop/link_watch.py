"""Live control-link watcher.

Answers one question while you drive: when control cuts out, is it the robot
resetting, the radio dropping packets, or the PC never sending them?

Compares the PC's outbound drive datagram counter against the ESP's received
counter once a second. Both climbing together = healthy link. PC climbing while
the ESP's stalls = packets dying on the air. ESP uptime going backwards = the
board rebooted (brownout under motor load).

    python link_watch.py [esp_ip]
"""

import json
import sys
import time
import urllib.request

ESP_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.104"
BRAIN = "http://127.0.0.1:8788/api/health"
ESP = f"http://{ESP_IP}:8765/api/status"


def get(url: str, timeout: float = 2.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - diagnostic, any failure is data
        return {"_err": str(exc)}


def main() -> None:
    prev_sent = prev_rx = prev_up = None
    misses = 0
    print(f"watching esp={ESP_IP} — drive now, hold the stick through a cutout")
    print("time      pc_tx/s  esp_rx/s  lost  up_s   note", flush=True)
    while True:
        brain = get(BRAIN)
        esp = get(ESP)
        now = time.strftime("%H:%M:%S")

        sent = (brain.get("drive_pump") or {}).get("udp_sent")
        rx = esp.get("udp_packets")
        up = esp.get("uptime_ms")
        up_s = "" if up is None else int(up / 1000)

        note = ""
        if "_err" in esp:
            misses += 1
            note = f"ESP UNREACHABLE x{misses} ({esp['_err'][:38]})"
        else:
            misses = 0
        if prev_up is not None and up is not None and up < prev_up:
            note = "*** ESP REBOOTED — brownout/reset ***"

        d_tx = sent - prev_sent if sent is not None and prev_sent is not None else 0
        d_rx = rx - prev_rx if rx is not None and prev_rx is not None else 0
        if not note and d_tx > 2:
            if d_rx == 0:
                note = "!! PC sending, ESP hearing NOTHING (radio/link)"
            elif d_rx < d_tx * 0.6:
                note = f"!! {100 - int(d_rx * 100 / max(d_tx, 1))}% packet loss"
        if not note and d_tx == 0:
            note = "(idle - no drive input)"

        print(
            f"{now}  {d_tx:>7}  {d_rx:>8}  {max(0, d_tx - d_rx):>4}  {up_s:>5}   {note}",
            flush=True,
        )

        if sent is not None:
            prev_sent = sent
        if rx is not None:
            prev_rx = rx
        if up is not None:
            prev_up = up
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
