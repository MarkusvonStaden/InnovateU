"""Where does a person stand? From a box in the front camera image to a point on the ground next to the car.

Frame of the result: base_link of the car = centre of the rear axle, x forward, y left (metres), like the rest
of the BITT stack. Two estimates, both assume a flat floor:

- height: a person is about 1.70 m tall, so depth = fy * 1.70 / box height. Used when the whole person is in
  the image. Off by as much as the person's height differs from 1.70 m (child: much too far).
- ground: the ray through the feet (bottom centre of the box) hits the floor. Used when the head is cut off,
  i.e. the person is close. The camera is only ~30 cm high, so further away 1 degree of pitch (car braking,
  bumps, mounting) already changes the distance by ~25 %.

When the feet are cut off at the bottom edge the person is closer than the floor seen at that edge: "edge" gives
that bound.
"""
import math
from dataclasses import dataclass, asdict

# Front camera of the BMW i8 ride-on: ros_ws/src/bitt_sim/config/bmw_kitt.urdf (camera_joint) and robot_config.yaml.
CAMERA_X = 0.92           # m in front of the rear axle
CAMERA_Y = 0.0301         # m to the left
CAMERA_HEIGHT = 0.30      # m above the floor (0.1875 above base_link + wheel radius 0.1125)
CAMERA_PITCH_UP = 0.085   # rad, the camera looks slightly up (URDF pitch -0.085)
FRONT_X = 1.05            # front bumper (length 1.27 - rear overhang 0.22)
HALF_WIDTH = 0.38         # half the car width
# Calibration of the left camera at 640x360 (robot_config.yaml) for when no CameraInfo arrives.
DEFAULT_K = dict(fx=551.84, fy=550.97, cx=341.68, cy=185.45, width=640, height=360)


@dataclass
class Camera:
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    x: float = CAMERA_X
    y: float = CAMERA_Y
    z: float = CAMERA_HEIGHT
    pitch_up: float = CAMERA_PITCH_UP

    def scaled(self, width, height):
        """The same camera for an image of another size (the preview is a scaled copy of the left image)."""
        sx, sy = width / self.width, height / self.height
        return Camera(self.fx * sx, self.fy * sy, self.cx * sx, self.cy * sy, width, height,
                      self.x, self.y, self.z, self.pitch_up)

    def ray(self, u, v):
        """Direction (forward, left, up) in base_link for pixel (u, v), not normalised."""
        fwd, left, up = 1.0, -(u - self.cx) / self.fx, -(v - self.cy) / self.fy
        c, s = math.cos(self.pitch_up), math.sin(self.pitch_up)
        return fwd * c - up * s, left, fwd * s + up * c

    def ground(self, u, v):
        """Floor point (x, y) seen at pixel (u, v), None if that pixel looks above the horizon."""
        f, l, up = self.ray(u, v)
        if up >= -1e-3:
            return None
        t = self.z / -up
        return self.x + t * f, self.y + t * l

    def by_height(self, u, v_top, v_bottom, person_height):
        """Floor point under a person of known height whose box spans v_top..v_bottom in column u."""
        depth = self.fy * person_height / max(v_bottom - v_top, 1.0)  # along the optical axis
        f, l, _ = self.ray(u, (v_top + v_bottom) / 2)  # optical-axis component of ray() is 1
        return self.x + depth * f, self.y + depth * l


@dataclass
class Person:
    x: float              # m in front of the rear axle (base_link)
    y: float              # m to the left (negative = right)
    distance: float       # m from the front bumper to the person, straight line on the floor
    angle_deg: float      # direction seen from the camera, positive = left
    zone: str             # "ahead" (in the car's lane), "left", "right"
    method: str           # "ground", "height" or "edge" (feet cut off: at most this far)
    conf: float           # detector confidence
    box: tuple            # (x1, y1, x2, y2) pixels

    def to_dict(self):
        d = asdict(self)
        for k in ("x", "y", "distance", "angle_deg", "conf"):
            d[k] = round(d[k], 2)
        d["box"] = [round(v) for v in self.box]
        return d


def locate(box, cam: Camera, conf=1.0, person_height=1.70, lane_margin=0.3, edge_px=3):
    """Box (x1, y1, x2, y2) of a person in an image of cam's size -> Person on the floor."""
    x1, y1, x2, y2 = box
    u = (x1 + x2) / 2
    feet_visible = y2 < cam.height - edge_px
    head_visible = y1 > edge_px

    if feet_visible and head_visible:
        point, method = cam.by_height(u, y1, y2, person_height), "height"
    elif feet_visible:
        point, method = cam.ground(u, y2) or cam.by_height(u, y1, y2, person_height), "ground"
    else:
        # Feet below the image: very close, at most as far as the floor seen at the bottom edge.
        point, method = cam.ground(u, cam.height - 1) or cam.by_height(u, y1, y2, person_height), "edge"

    x, y = point
    angle = math.degrees(math.atan2(-(u - cam.cx), cam.fx))
    zone = "ahead" if abs(y) <= HALF_WIDTH + lane_margin else ("left" if y > 0 else "right")
    distance = math.hypot(max(x - FRONT_X, 0.0), max(abs(y) - HALF_WIDTH, 0.0))
    return Person(x, y, distance, angle, zone, method, conf, tuple(box))
