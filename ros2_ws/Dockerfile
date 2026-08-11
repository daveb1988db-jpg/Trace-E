FROM ros:jazzy-ros-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-opencv python3-numpy \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ws
COPY ros2_ws/src/trace_e_follow /ws/src/trace_e_follow
COPY desktop/follow_person.py /ws/src/trace_e_follow/trace_e_follow/follow_person.py

ENV PYTHONPATH=/ws/src/trace_e_follow/trace_e_follow:/ws/src/trace_e_follow
