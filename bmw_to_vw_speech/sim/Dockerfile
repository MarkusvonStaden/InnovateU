# Fake BITT: the car's ROS side as far as BMWtoVWSpeech sees it. Real ROS 2 Lyrical with a Foxglove bridge on
# port 8765 (like the car) and, optionally, a recorded bag playing in a loop so Foxglove shows camera, TF, ...
# Build context = repository root (for car_interfaces). Started by sim/docker-compose.yml.

ARG BASE_IMAGE=ros:lyrical-ros-base
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-lyrical-foxglove-bridge \
        ros-lyrical-rosbag2 \
        ros-lyrical-rosbag2-storage-mcap \
        python3-colcon-common-extensions \
    && rm -rf /var/lib/apt/lists/*

# The car's own message types, so bags with /lights_cmd, /steer_state, ... replay and show up decoded.
COPY ros_ws/src/car_interfaces /opt/bitt_ws/src/car_interfaces
RUN bash -c "source /opt/ros/lyrical/setup.bash && cd /opt/bitt_ws && colcon build" \
    && rm -rf /opt/bitt_ws/build /opt/bitt_ws/log

# ros2 available in `docker compose exec fake-bitt bash -ic "ros2 topic echo ..."`.
RUN printf '%s\n' 'source /opt/ros/lyrical/setup.bash' 'source /opt/bitt_ws/install/setup.bash' >> /root/.bashrc

COPY bmw_to_vw_speech/sim/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
