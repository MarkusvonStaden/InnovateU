import json
import math

import numpy as np
import pytest

from person_finder.__main__ import Latest, to_bgr
from person_finder.cdr import Codec
from person_finder.geometry import DEFAULT_K, FRONT_X, Camera, locate

CAM = Camera(**DEFAULT_K)
LEVEL = Camera(**DEFAULT_K, pitch_up=0.0)


def test_ground_point_straight_ahead():
    # Level camera 0.30 m high: a pixel 0.1*fy below the centre sees the floor 3.0 m ahead.
    x, y = LEVEL.ground(LEVEL.cx, LEVEL.cy + 0.1 * LEVEL.fy)
    assert x - LEVEL.x == pytest.approx(3.0)
    assert y == pytest.approx(LEVEL.y)


def test_above_horizon_is_not_on_the_floor():
    assert CAM.ground(CAM.cx, 0) is None


def test_left_is_positive_y():
    x, y = LEVEL.ground(LEVEL.cx - 0.1 * LEVEL.fx, LEVEL.cy + 0.1 * LEVEL.fy)
    assert y - LEVEL.y == pytest.approx(0.3)  # 0.1 * 3 m


def test_height_method():
    # 1.70 m tall, 170 px high -> depth = fy * 1.7 / 170 = fy / 100
    p = locate((300, 100, 340, 270), LEVEL)
    assert p.method == "height"
    assert p.x - LEVEL.x == pytest.approx(LEVEL.fy / 100, rel=1e-6)


def test_head_cut_off_uses_floor_and_feet_cut_off_is_a_bound():
    near = locate((250, 0, 400, 330), CAM)
    assert near.method == "ground"
    closest = locate((200, 0, 450, 360), CAM)
    assert closest.method == "edge"
    assert closest.x < near.x


def test_zones_and_distance():
    ahead = locate((CAM.cx - 20, 0, CAM.cx + 20, 330), CAM)
    assert ahead.zone == "ahead"
    assert ahead.distance == pytest.approx(ahead.x - FRONT_X)
    assert locate((0, 100, 30, 200), CAM).zone == "left"
    assert locate((610, 100, 640, 200), CAM).zone == "right"
    assert locate((0, 100, 30, 200), CAM).angle_deg > 0


def test_scaled_camera_gives_same_answer_for_scaled_box():
    small = CAM.scaled(320, 180)
    a, b = locate((300, 100, 340, 270), CAM), locate((150, 50, 170, 135), small)
    assert (a.x, a.y) == pytest.approx((b.x, b.y))


def test_to_dict_is_json():
    d = locate((300, 100, 340, 270), CAM, conf=0.812345).to_dict()
    assert json.loads(json.dumps(d))["conf"] == 0.81


def test_cdr_string_and_image_round_trip():
    codec = Codec()
    assert codec.decode("std_msgs/String", codec.encode("std_msgs/String", {"data": "hi"})) == {"data": "hi"}
    header = {"stamp": {"sec": 5, "nanosec": 7}, "frame_id": "left_optical"}
    payload = codec.encode("sensor_msgs/CompressedImage", {"header": header, "format": "jpeg", "data": b"\xff\xd8"})
    assert codec.decode("sensor_msgs/CompressedImage", payload)["header"] == header


def test_camera_info_decodes_k():
    codec = Codec()
    k = [500.0, 0, 320, 0, 501.0, 180, 0, 0, 1]
    payload = codec.encode("sensor_msgs/CameraInfo", {"height": 360, "width": 640, "distortion_model": "plumb_bob",
                                                      "d": [0.1, 0.2], "k": k})
    info = codec.decode("sensor_msgs/CameraInfo", payload)
    assert info["k"] == k and info["width"] == 640


def test_raw_mono8_with_padding_to_bgr():
    codec = Codec()
    rows = np.arange(2 * 4, dtype=np.uint8).reshape(2, 4)  # step 4, width 3: last column is padding
    msg = codec.decode("sensor_msgs/Image", codec.encode("sensor_msgs/Image", {
        "height": 2, "width": 3, "encoding": "mono8", "step": 4, "data": rows.tobytes()}))
    img = to_bgr("sensor_msgs/Image", msg)
    assert img.shape == (2, 3, 3)
    assert img[1, 2].tolist() == [6, 6, 6]


def test_latest_keeps_only_newest():
    latest = Latest()
    latest.put(1)
    latest.put(2)
    assert latest.take(0) == 2
    assert latest.take(0) is None


def test_bearing_matches_pixel():
    p = locate((CAM.cx + CAM.fx - 5, 100, CAM.cx + CAM.fx + 5, 200), CAM)
    assert p.angle_deg == pytest.approx(-45, abs=0.5)
    assert math.isfinite(p.distance)
