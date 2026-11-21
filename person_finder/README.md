# Person Finder

Where do people stand in front of the car? Front camera → YOLO → position on the floor, published back to the car.

Runs on a laptop **without ROS**: it talks to the car's `foxglove_bridge` over Wi-Fi, like
[rear_buddy](../student_ws/src/rear_buddy) (whose CDR codec and bridge client it copies).

```
🚶 3 Personen (24 ms)
   - 1.3 m, vor dem Auto (+0.0 m seitlich, +0°)
   - 2.6 m, rechts (-0.9 m seitlich, -19°)
   - 4.3 m, links (+1.3 m seitlich, +17°)
```

## Setup and run

```bash
cd person_finder
uv sync                                                # first run also downloads yolo11n.pt (5 MB)
uv run person-finder                                   # real car: ws://10.42.0.1:8765
uv run person-finder --bridge ws://localhost:8765      # fake BITT with a bag (bmw_to_vw_speech/README.md)
uv run person-finder --bridge ws://localhost:8766      # Gazebo sim, if it runs on port 8766
uv run pytest
```

`BITT_BRIDGE` in a `.env` (see `.env.example`) replaces `--bridge`.

In Foxglove, open an Image panel on **`/persons/image/compressed`**: the camera picture with a box and the distance
on every person (red = in front of the car, yellow = to the side).

## Topics

| | Topic | Type |
|---|---|---|
| in | `/preview/left/compressed` (car, bags), otherwise `/visual_slam/image_0` (Gazebo) | `sensor_msgs/CompressedImage` / `Image` |
| in | `/visual_slam/camera_info_0` (calibration; built-in values if missing) | `sensor_msgs/CameraInfo` |
| out | `/persons` | `std_msgs/String` with JSON, see below |
| out | `/persons/image/compressed` | `sensor_msgs/CompressedImage` (JPEG) |

```json
{"stamp": 1795183749.5, "frame_id": "base_link",
 "persons": [{"x": 3.35, "y": -0.9, "distance": 2.38, "angle_deg": -16.2, "zone": "right",
              "method": "height", "conf": 0.79, "box": [450, 64, 530, 281]}]}
```

| Field | Meaning |
|---|---|
| `x`, `y` | floor position in `base_link`: metres in front of the rear axle, metres to the left (negative = right) |
| `distance` | metres from the front bumper to the person |
| `angle_deg` | direction seen from the camera, positive = left |
| `zone` | `ahead` (within the car's width + 0.3 m), `left`, `right` |
| `method` | how the distance was estimated, see below |
| `conf`, `box` | YOLO confidence, box in pixels of the camera picture |

Persons are sorted by distance, nearest first. An empty list means nobody is visible.

## How the position is estimated

One camera has no depth, so the distance comes from geometry (camera pose from
[bmw_kitt.urdf](../ros_ws/src/bitt_sim/config/bmw_kitt.urdf): 0.92 m in front of the rear axle, 0.30 m above the
floor, tilted 4.9° up):

- **`height`** (whole person in the picture): a person is about 1.70 m tall, so the box height gives the depth.
  A child or someone sitting is placed too far away; `--person-height` changes the assumption.
- **`ground`** (head cut off, i.e. close): the bottom of the box is where the feet touch the floor.
- **`edge`** (feet cut off too, very close): the person is *at most* this far away. The console shows "höchstens".

Further away the floor method is not reliable: the camera is so low that 1° of pitch (braking, bumps) already
changes the distance by about 25 %. That is why it is only used close up.

**Checked:** in the Gazebo street scene (car at the origin, pedestrian crossing at x = 8.5 m, i.e. 7.45 m from the
bumper) it reports 7.7–8.0 m and follows the person from right to left. In the KICU_0 bag the distances look
plausible but there is no ground truth.

## Things to know

- YOLO (`yolo11n`, COCO class *person*) sometimes takes coats on chairs etc. for a person. `--conf 0.5` filters
  more, but also misses more.
- About 25 ms per picture on an M-series Mac (MPS). The preview camera has ~5 pictures/s, so every one is used;
  with the raw 60 Hz topic `--max-hz` (default 10) skips pictures. Never subscribe to raw images over Wi-Fi.
- `--show` opens a window with the picture, `--no-publish` only prints.
- The person's position is in the car's frame, not on a map: for map coordinates combine it with `/tf`.

## Files

| File | |
|---|---|
| `person_finder/__main__.py` | CLI: subscribe, detect, print, publish |
| `person_finder/geometry.py` | box → floor position (camera pose, zones) |
| `person_finder/detector.py` | YOLO wrapper |
| `person_finder/foxglove_client.py`, `cdr.py` | bridge client and ROS message (de)serialisation, from rear_buddy |
