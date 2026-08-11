#!/bin/bash
export MAMBA_ROOT_PREFIX="$HOME/micromamba"
"$HOME/bin/micromamba" run -n ros2 ros2 --help | head -15
"$HOME/bin/micromamba" run -n ros2 printenv ROS_DISTRO
echo VERIFY_OK