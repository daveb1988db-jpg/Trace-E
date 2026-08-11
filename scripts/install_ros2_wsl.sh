#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/bin" "$HOME/micromamba"
cd /tmp
if [[ ! -x "$HOME/bin/micromamba" ]]; then
  curl -fsSL -o micromamba.tar.bz2 https://micro.mamba.pm/api/micromamba/linux-64/latest
  tar -xjf micromamba.tar.bz2 bin/micromamba
  mv -f bin/micromamba "$HOME/bin/micromamba"
fi
"$HOME/bin/micromamba" --version
export MAMBA_ROOT_PREFIX="$HOME/micromamba"
eval "$("$HOME/bin/micromamba" shell hook -s bash)"
if ! micromamba env list | grep -qE '^[[:space:]]*ros2[[:space:]]'; then
  micromamba create -y -n ros2 -c conda-forge ros-humble-ros-base python=3.11
fi
micromamba run -n ros2 ros2 --help >/dev/null
echo ROS_ENV_OK
