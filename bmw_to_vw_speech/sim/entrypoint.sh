#!/bin/bash
# Starts the Foxglove bridge (port 8765) and, if BAG names a directory under /bags, replays it in a loop.
set -e
source /opt/ros/lyrical/setup.bash
source /opt/bitt_ws/install/setup.bash

if [ -n "$BAG" ]; then
    if [ ! -d "/bags/$BAG" ]; then
        echo "fake-bitt: bag '$BAG' not found in bags/. Available:" >&2
        ls /bags >&2
        exit 1
    fi
    echo "fake-bitt: replaying bags/$BAG in a loop"
    ros2 bag play "/bags/$BAG" --loop --progress-bar-update-rate 0 $BAG_ARGS &
fi

echo "fake-bitt: Foxglove bridge on ws://localhost:8765"
exec ros2 launch foxglove_bridge foxglove_bridge_launch.xml port:=8765
