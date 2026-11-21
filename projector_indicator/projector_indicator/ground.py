"""The ground in front of the car for the projection: where things in base_link land on the light patch, and the path
the car is about to drive.

    ground = Ground(calibration, start_m=1.05)            # calibration from projector_cal.py
    ground.to_world(2.5, -0.3)                            # base_link metres -> mm on the projected ground
    corridor_contains(2.5, 0.1, steer_rad=0.2)            # is that point in the car's path?

base_link: origin in the middle of the rear axle, x ahead, y to the left, metres. World (the page's drawing surface):
millimetres, x to the car's right, y towards the car. The light patch's edge next to the car is at base_link
x = start_m, its middle (the middle of the footprint's nearest edge) at y = offset_m.
"""
import math

WHEELBASE_M = 0.72      # config/robot_config.yaml
CAR_WIDTH_M = 0.76      # body width (bmw_kitt.urdf)
FRONT_M = 1.05          # base_link x of the front bumper


class Ground:
    def __init__(self, calibration, start_m=FRONT_M, offset_m=0.0):
        self.footprint = [tuple(p) for p in calibration['footprint_mm']]
        near = sorted(self.footprint, key=lambda p: -p[1])[:2]
        self.origin = ((near[0][0] + near[1][0]) / 2, (near[0][1] + near[1][1]) / 2)
        self.start_m, self.offset_m = start_m, offset_m

    def to_world(self, x, y):
        ox, oy = self.origin
        return ox - (y - self.offset_m) * 1000, oy - (x - self.start_m) * 1000

    def to_base(self, wx, wy):
        ox, oy = self.origin
        return self.start_m + (oy - wy) / 1000, self.offset_m + (ox - wx) / 1000

    def contains(self, wx, wy, margin=0.0):
        """Is the world point inside the light patch, at least margin mm from its edges?"""
        poly = self.footprint
        area2 = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))
        sign = 1 if area2 > 0 else -1
        for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
            length = math.hypot(x2 - x1, y2 - y1) or 1
            # distance to the edge, positive inside
            if sign * ((x2 - x1) * (wy - y1) - (y2 - y1) * (wx - x1)) / length < margin:
                return False
        return True


def corridor_contains(x, y, steer_rad, width_m=CAR_WIDTH_M, wheelbase_m=WHEELBASE_M, front_m=FRONT_M,
                      length_m=8.0):
    """Is the base_link point (x, y) in the path the car drives with this steering angle (bicycle model: the rear
    axle's centre moves on a circle of radius wheelbase / tan(steer)), ahead of its front bumper?"""
    k = math.tan(steer_rad) / wheelbase_m        # curvature, + = to the left
    if abs(k) < 1e-4:
        return front_m <= x <= front_m + length_m and abs(y) <= width_m / 2
    r = 1 / k
    off = abs(math.hypot(x, y - r) - abs(r))     # distance from the rear axle's circle
    angle = math.atan2(x, (r - y) * math.copysign(1, r))   # how far along the circle, from the rear axle
    return off <= width_m / 2 and 0 <= angle * abs(r) <= front_m + length_m and x >= front_m - 0.05


class Odometry:
    """Where the car is, by dead reckoning from the wheel speed (/wheel_odom) and the steering angle (bicycle model:
    turn rate = speed * tan(steer) / wheelbase), in its own frame: (0, 0) heading 0 where it was first heard from.
    Only the motion matters (things on the ground are pinned in this frame), so its drift over a few metres is harmless."""

    def __init__(self, wheelbase_m=WHEELBASE_M, max_gap_s=0.5):
        self.wheelbase_m, self.max_gap_s = wheelbase_m, max_gap_s
        self.x = self.y = self.yaw = self.v = self.w = 0.0
        self.at = None

    def update(self, speed_mps, steer_rad, t):
        w = speed_mps * math.tan(steer_rad) / self.wheelbase_m
        if self.at is not None and 0 < t - self.at <= self.max_gap_s:   # after a longer gap: nothing known in between
            dt = t - self.at
            mid = self.yaw + w * dt / 2
            self.x += speed_mps * math.cos(mid) * dt
            self.y += speed_mps * math.sin(mid) * dt
            self.yaw += w * dt
        self.v, self.w, self.at = speed_mps, w, t

    def pose(self, now, stale_s=1.0):
        """For the page: position (m), heading (rad), speed (m/s) and turn rate (rad/s), or None when stale."""
        if self.at is None or now - self.at > stale_s:
            return None
        return {'x': round(self.x, 4), 'y': round(self.y, 4), 'yaw': round(self.yaw, 5),
                'v': round(self.v, 3), 'w': round(self.w, 4)}
