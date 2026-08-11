#!/usr/bin/env python3
"""ROS2-flavoured wrapper: same PersonFollower as speak_server."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

# Allow importing sibling follow_person.py (copied in Docker or from desktop/)
HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parents[3] / "desktop" if (HERE.parents[3] / "desktop").is_dir() else HERE
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(DESKTOP))

from follow_person import FOLLOWER  # noqa: E402


def main() -> int:
    esp = os.environ.get("TRACE_E_ESP_BASE") or "http://192.168.1.104"
    print(f"[trace_e_follow] esp={esp}", flush=True)
    FOLLOWER.start(esp_base=esp)

    stop = False

    def _sig(*_a):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    while not stop:
        st = FOLLOWER.status()
        print(
            f"[follow] mode={st.get('mode')} person={st.get('person')} "
            f"L={st.get('left')} R={st.get('right')} fps={st.get('fps')}",
            flush=True,
        )
        time.sleep(0.5)

    FOLLOWER.stop("ros node shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
