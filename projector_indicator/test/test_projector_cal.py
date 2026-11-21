"""Tests for the camera calibration of the projection, on a made-up room: python3 -m pytest test/"""
import json

import cv2
import numpy as np
import pytest

from projector_indicator.indicator import load_calibration, page_query
from projector_indicator.projector_cal import (GRID, largest_rectangle, order_clockwise, paper_homography, plausibility,
                                       solve)

SCREEN = (2560, 1440)
IMAGE = (640, 480)


def camera_homography(f=550.0):
    """ground (mm, x right / y down seen from above, so +z points into the floor) -> image of a real (not mirrored)
    camera 1.2 m above the floor, 1.8 m away, looking at the projection."""
    k = np.array([[f, 0, 320], [0, f, 240], [0, 0, 1]])
    centre = np.array([400.0, 1800.0, -1200.0])
    ahead = np.array([400.0, 0, 0]) - centre
    ahead /= np.linalg.norm(ahead)
    down = np.array([0, 0, 1.0]) - ahead * ahead[2]
    down /= np.linalg.norm(down)
    r = np.array([np.cross(down, ahead), down, ahead])  # rows: camera x (right), y (down), z (ahead)
    assert np.linalg.det(r) > 0
    t = -r @ centre
    return k @ np.column_stack([r[:, 0], r[:, 1], t])


def apply(h, pts):
    return cv2.perspectiveTransform(np.float64(pts).reshape(-1, 1, 2), h).reshape(-1, 2)


# Projector at the ground point (1000, 0) shining towards -x: its screen u runs along the throw (u = 1 next to it),
# v across. Strong keystone: 300 mm wide next to it, 1200 mm at the far end, 1500 mm long.
NEAR, FAR, LENGTH = 300.0, 1200.0, 1500.0
HS2G = cv2.getPerspectiveTransform(
    np.float32([[0, 0], [SCREEN[0], 0], [SCREEN[0], SCREEN[1]], [0, SCREEN[1]]]),
    np.float32([[1000 - LENGTH, FAR / 2], [1000, NEAR / 2], [1000, -NEAR / 2], [1000 - LENGTH, -FAR / 2]]))


def measurement(f=550.0):
    hg2c = camera_homography(f)
    found = []
    for v in GRID:
        for u in GRID:
            x, y = apply(hg2c, apply(HS2G, [[u * SCREEN[0], v * SCREEN[1]]]))[0]
            found.append((u, v, x, y))
    paper_ground = [[200, -105], [497, -105], [497, 105], [200, 105]]  # A4 lying lengthwise, inside the light
    return found, apply(hg2c, paper_ground)


def test_corners_are_put_in_clockwise_order():
    q = order_clockwise([[10, 10], [0, 10], [0, 0], [10, 0]])
    assert q.tolist() == [[0, 0], [10, 0], [10, 10], [0, 10]]


def test_paper_side_lengths_are_found_out():
    found, paper = measurement()
    hc2g, f = paper_homography(paper, (297, 210), IMAGE)
    g = apply(hc2g, paper)
    sides = sorted(np.linalg.norm(g - np.roll(g, -1, axis=0), axis=1))
    assert sides == pytest.approx([210, 210, 297, 297], abs=0.5)
    assert f == pytest.approx(550, rel=0.02)


def test_largest_rectangle_in_a_trapezoid():
    # half width 1 + 0.4 x: the best rectangle starts at x = 3.75 with half width 2.5
    x0, x1, c, half = largest_rectangle([[0, -1], [10, -5], [10, 5], [0, 1]])
    assert (x0, x1, c, half) == pytest.approx((3.75, 10, 0, 2.5), abs=0.15)


def world_to_true_ground(cal):
    """world mm -> the made-up room's ground, through the screen (the way the projector really maps it)."""
    w2s = np.diag([SCREEN[0], SCREEN[1], 1]) @ np.array(cal['world_to_screen']).reshape(3, 3)
    return HS2G @ w2s


def test_solve_maps_millimetres_undistorted_onto_the_whole_light_patch():
    found, paper = measurement()
    cal, info = solve(found, paper, IMAGE, SCREEN)
    assert info['inliers'] == len(GRID) ** 2 and info['residual_mm'] < 1
    m = world_to_true_ground(cal)
    # 1 world mm = 1 mm on the ground, in every direction and everywhere: a 100 mm square stays one
    for x, y in ((100, 100), (cal['size_mm'][0] / 2, cal['size_mm'][1] / 2), (500, 1300)):
        o, ex, ey = apply(m, [[x, y], [x + 100, y], [x, y + 100]])
        assert np.linalg.norm(ex - o) == pytest.approx(100, abs=1.5)
        assert np.linalg.norm(ey - o) == pytest.approx(100, abs=1.5)
        assert abs(np.dot(ex - o, ey - o)) < 2 * 100
    # the whole light patch is used: its bounding box on the ground is the world
    assert cal['size_mm'] == pytest.approx([FAR, LENGTH], abs=15)
    foot = apply(m, cal['footprint_mm'])
    true_patch = apply(HS2G, [[0, 0], [SCREEN[0], 0], [SCREEN[0], SCREEN[1]], [0, SCREEN[1]]])
    assert sorted(map(tuple, foot.round(-1))) == sorted(map(tuple, true_patch.round(-1)))
    # the right way round: world y grows towards the projector (the car), world x to the car's right
    front_left, front_right, back_left = apply(m, [[0, 0], [cal['size_mm'][0], 0], [0, cal['size_mm'][1]]])
    assert front_left[0] < back_left[0]          # the projector is at +x, front is away from it
    assert front_left[1] > front_right[1]        # facing -x in a y-down map, the car's left is +y


def test_indicator_box_is_inside_the_light_patch():
    found, paper = measurement()
    cal, _ = solve(found, paper, IMAGE, SCREEN)
    x, y, w, length = cal['box_mm']
    assert length / w == pytest.approx(0.4, rel=0.05)
    patch = np.float32(cal['footprint_mm']).reshape(-1, 1, 2)
    for corner in ((x, y), (x + w, y), (x + w, y + length), (x, y + length)):
        assert cv2.pointPolygonTest(patch, (float(corner[0]), float(corner[1])), False) >= 0
    assert w > NEAR   # it found the wide part of the patch, ahead


def test_wall_squares_limit_the_floor_and_the_indicator_but_not_the_drawing_surface():
    found, paper = measurement()
    found = [(u, v, x + (200 if u == GRID[0] else 0), y - (150 if u == GRID[0] else 0)) for u, v, x, y in found]
    cal, info = solve(found, paper, IMAGE, SCREEN)
    assert info['inliers'] == len(GRID) ** 2 - len(GRID)
    m = world_to_true_ground(cal)
    last_on_ground = apply(HS2G, [[GRID[1] * SCREEN[0], SCREEN[1] / 2]])[0, 0]
    assert apply(m, cal['floor_mm'])[:, 0].min() == pytest.approx(last_on_ground, abs=5)   # measured floor ends there
    assert apply(m, cal['footprint_mm'])[:, 0].min() == pytest.approx(1000 - LENGTH, abs=15)  # the whole screen is used
    x, y, w, length = cal['box_mm']
    floor = np.float32(cal['floor_mm']).reshape(-1, 1, 2)
    for corner in ((x, y), (x + w, y), (x + w, y + length), (x, y + length)):  # the indicator stays on the floor
        assert cv2.pointPolygonTest(floor, (float(corner[0]), float(corner[1])), False) >= 0


def test_the_light_patch_is_capped_before_the_horizon():
    # the same projector tilted up so far that the top of its picture would reach the horizon
    flat = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [SCREEN[0], 0], [SCREEN[0], SCREEN[1]], [0, SCREEN[1]]]),
        np.float32([[-40000, 9000], [1000, NEAR / 2], [1000, -NEAR / 2], [-40000, -9000]]))
    hg2c = camera_homography()
    found = [(u, v, *apply(hg2c, apply(flat, [[u * SCREEN[0], v * SCREEN[1]]]))[0])
             for v in GRID for u in GRID if u > 0.5]   # the camera only sees the near half
    _, paper = measurement()
    cal, info = solve(found, paper, IMAGE, SCREEN, max_mm=6000)
    assert max(cal['size_mm']) <= 6000 * 2.1
    assert 0 < info['whole_uv'][0] < 0.5   # grown towards the far edge, but not all the way


def test_page_and_file(tmp_path):
    found, paper = measurement()
    cal, _ = solve(found, paper, IMAGE, SCREEN)
    query = page_query(90, True, True, 0.5, calibration=cal)
    assert query.startswith('?ground=') and query.endswith('&calibrate=1') and 'rotate' not in query
    path = tmp_path / 'cal.json'
    path.write_text(json.dumps(cal))
    assert load_calibration(path) == cal
    assert load_calibration(tmp_path / 'missing.json') is None
    path.write_text('{"quad": [1, 2]}')   # an old calibration
    assert load_calibration(path) is None


def test_implausible_results_are_not_saved():
    found, paper = measurement()
    cal, info = solve(found, paper, IMAGE, SCREEN)
    assert plausibility(cal, info) is None
    assert 'light patch' in plausibility({**cal, 'floor_mm': [[0, 0], [5152, 0], [5152, 23476], [0, 23476]]}, info)
    assert 'squares' in plausibility(cal, {**info, 'inliers': 3})


def test_a_misread_paper_stops_the_calibration():
    _, paper = measurement()
    with pytest.raises(SystemExit):
        paper_homography(paper + [[0, 0], [0, 0], [25, -9], [0, 0]], (297, 210), IMAGE)
