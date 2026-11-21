#!/usr/bin/env python3
"""Calibrate the indicator projection with a camera: measures where the projector's pixels land on the ground, so
the page can draw on the whole light patch in millimetres, undistorted, the right way round and not mirrored.

    python3 -m projector_indicator.projector_cal                 # measure, save, show the result (calibration grid)
    python3 -m projector_indicator.projector_cal --camera 1      # another camera
    python3 -m projector_indicator.indicator                     # uses the saved calibration from now on

Set up first:
  - projector on (2nd screen, "Extend" with Win+P), room as dark as you can,
  - a camera (the laptop's webcam is fine) that sees the whole light patch on the ground, from as high as possible,
  - one sheet of A4 paper flat on the ground inside the light patch, all four corners visible to the camera,
  - nobody walking through the picture for about 30 s.

How: the projector shows black, white and then single squares at known screen positions; the camera finds them.
The paper (known size) gives the ground in millimetres, so every square gets a position on the ground: that is the
projector -> ground mapping. Ahead of the car = away from the projector (the narrow end of the light patch is next to
it). The page then draws in ground millimetres (x to the car's right, y towards the car) and maps them onto the screen
with that mapping, so everything lands undistorted on the whole light patch. Squares the camera finds off the ground
plane (on a wall) are left out, and so is the part of the light patch around them.
Saved to projector_indicator/projector_local.json (committed: the measurement of our set-up); afterwards it projects the grid and saves a top view of it.
"""
import argparse
import json
import pathlib
import subprocess
import tempfile
import time

import cv2
import numpy as np

from .display_server import DisplayServer
from .indicator import CALIBRATION, PAGE, page_query
from .screens import browser_command, find_browser, monitors, pick_screen

PROBE = """<!doctype html><html translate="no"><head><meta charset="utf-8"><meta name="google" content="notranslate">
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden;cursor:none}body.white{background:#fff}
#d{position:fixed;width:11vmin;height:11vmin;background:#fff;transform:translate(-50%,-50%);display:none}</style>
</head><body><div id="d"></div><script>const d=document.getElementById('d');
new EventSource('events').onmessage=e=>{const s=JSON.parse(e.data);document.body.className=s.mode==='white'?'white':'';
d.style.display=s.mode==='dot'?'block':'none';if(s.mode==='dot'){d.style.left=s.u*100+'vw';d.style.top=s.v*100+'vh';}};
</script></body></html>"""
GRID = (0.06, 0.28, 0.5, 0.72, 0.94)


# ---- geometry (pure, tested) ------------------------------------------------------------------------------------

def order_clockwise(quad):
    """Corners clockwise in image coordinates (y down), starting at the top-left-most one."""
    q = np.asarray(quad, np.float64)
    c = q.mean(axis=0)
    q = q[np.argsort(np.arctan2(q[:, 1] - c[1], q[:, 0] - c[0]))]  # increasing angle = clockwise when y is down
    start = np.argmin(q.sum(axis=1))
    return np.roll(q, -start, axis=0)


def focal_from_plane(hg2c, center):
    """Focal length (pixels) that makes a ground->image homography a real camera: from the 'equal length' condition
    on its first two columns. None when no positive focal length fits (the paper sides are the other way round)."""
    t = np.array([[1, 0, -center[0]], [0, 1, -center[1]], [0, 0, 1]], np.float64)
    h1, h2 = (t @ hg2c)[:, 0], (t @ hg2c)[:, 1]
    den = h2[2] ** 2 - h1[2] ** 2
    if abs(den) < 1e-12:
        return None
    f2 = (h1[0] ** 2 + h1[1] ** 2 - h2[0] ** 2 - h2[1] ** 2) / den
    return float(np.sqrt(f2)) if f2 > 0 else None


def paper_homography(corners, size_mm, image_size, typical_f=None):
    """camera -> ground (mm) from the paper's corners, for a real camera (square pixels, centred, one focal length).
    Which side is the long one is decided by which way round gives a plausible focal length. A free homography from
    the four corners of a small sheet fits the sheet just as well, but extrapolates wildly further away."""
    q = order_clockwise(corners).astype(np.float64)
    a, b = size_mm
    w, h = image_size
    typical_f = typical_f or 0.8 * w
    best = None
    for long_first in (True, False):
        x, y = (a, b) if long_first else (b, a)
        g = np.float64([[0, 0], [x, 0], [x, y], [0, y]])           # clockwise too, so ground is "image-like" from above
        free = cv2.getPerspectiveTransform(q.astype(np.float32), g.astype(np.float32))
        f = focal_from_plane(np.linalg.inv(free), (w / 2, h / 2))
        score = abs(np.log(f / typical_f)) if f else np.inf
        if best is None or score < best[0]:
            best = (score, g, f)
    _, g, f = best
    if f is None or not 0.5 * w <= f <= 1.6 * w:
        raise SystemExit('the paper does not look like a flat sheet of that size to the camera (corners hidden or '
                         'misread, sheet not flat, bright patches next to it?): fix that and measure again')
    k = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]])
    ok, rvec, tvec = cv2.solvePnP(np.column_stack([g, np.zeros(4)]), q, k, None, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        raise SystemExit('could not work out the camera from the paper')
    r = cv2.Rodrigues(rvec)[0]
    hg2c = k @ np.column_stack([r[:, 0], r[:, 1], tvec.ravel()])
    return np.linalg.inv(hg2c), f


def footprint_frame(hs2g, corners):
    """Car frame on the ground from the projector -> ground mapping and a measured screen rectangle (4 corners in
    pixels, in order): origin in the middle of its edge next to the projector (the shortest on the ground), unit
    vectors ahead and to the car's left."""
    c = to_ground(hs2g, corners)
    edges = [(i, (i + 1) % 4) for i in range(4)]
    near = min(edges, key=lambda e: np.linalg.norm(c[e[0]] - c[e[1]]))
    far = ((near[0] + 2) % 4, (near[1] + 2) % 4)
    origin = (c[near[0]] + c[near[1]]) / 2
    ahead = (c[far[0]] + c[far[1]]) / 2 - origin
    ahead /= np.linalg.norm(ahead)
    left = np.array([ahead[1], -ahead[0]])  # ground frame is y-down seen from above, like an image
    return origin, ahead, left


def to_ground(hs2g, pts):
    return cv2.perspectiveTransform(np.float64(pts).reshape(-1, 1, 2), hs2g).reshape(-1, 2)


def largest_rectangle(polygon, aspect=None, steps=49):
    """Largest rectangle with sides along the axes inside a convex polygon (x = ahead, y = left), optionally with a
    fixed length/width. Returns (x0, x1, y_center, half_width)."""
    p = np.asarray(polygon, np.float64)
    d1, d2 = p[1] - p[0], p[2] - p[1]
    if d1[0] * d2[1] - d1[1] * d2[0] < 0:
        p = p[::-1]
    # counter-clockwise now: outward normal of edge r -> q is (dy, -dx); inside means a.p <= a.r for every edge
    a = np.array([[q[1] - r[1], r[0] - q[0]] for r, q in zip(p, np.roll(p, -1, axis=0))])
    b = np.einsum('ij,ij->i', a, p)
    xs, ys = np.linspace(p[:, 0].min(), p[:, 0].max(), steps), np.linspace(p[:, 1].min(), p[:, 1].max(), steps)
    best = (0.0, 0, 0, 0, 0)
    for i, x0 in enumerate(xs):
        for x1 in xs[i + 1:]:
            for c in ys:
                hmax = np.inf
                for x in (x0, x1):
                    for sign in (1, -1):
                        rhs = b - a[:, 0] * x - a[:, 1] * c
                        if (rhs < 0).any():
                            hmax = -1
                            break
                        coef = sign * a[:, 1]
                        pos = coef > 1e-12
                        if pos.any():
                            hmax = min(hmax, (rhs[pos] / coef[pos]).min())
                    if hmax < 0:
                        break
                if hmax <= 0 or not np.isfinite(hmax):
                    continue
                if aspect:
                    hmax = min(hmax, (x1 - x0) / aspect / 2)
                    if (x1 - x0) > aspect * 2 * hmax + 1e-9:
                        continue
                area = (x1 - x0) * 2 * hmax
                if area > best[0]:
                    best = (area, x0, x1, c, hmax)
    return best[1:]


def rect_corners(r, size):
    """Screen rectangle (u0, v0, u1, v1) in fractions -> its 4 corners in pixels, clockwise from top left."""
    u0, v0, u1, v1 = r
    w, h = size
    return [[u0 * w, v0 * h], [u1 * w, v0 * h], [u1 * w, v1 * h], [u0 * w, v1 * h]]


def extend_to_screen(hs2g, r, size, origin, ahead, max_mm=6000.0):
    """Grow the measured screen rectangle r = (u0, v0, u1, v1) to the whole screen, side by side, as far as the
    projector -> ground mapping stays sensible: in front of the projector's horizon and at most max_mm away.
    Beyond the measured squares it is extrapolated, which works because the projection is a plane-to-plane map."""
    w, h = size
    centre = np.array([(r[0] + r[2]) / 2 * w, (r[1] + r[3]) / 2 * h, 1.0])
    sign = np.sign(hs2g[2] @ centre)

    def sensible(rect):
        c = np.array(rect_corners(rect, size))
        pts = np.concatenate([np.linspace(c[i], c[(i + 1) % 4], 9) for i in range(4)])
        den = np.column_stack([pts, np.ones(len(pts))]) @ hs2g[2]
        if (den * sign <= 1e-9 * np.abs(den).max()).any():
            return False  # at or beyond the horizon: those pixels never reach the ground
        g = to_ground(hs2g, pts) - origin
        return np.abs(g @ ahead).max() <= max_mm and np.linalg.norm(g, axis=1).max() <= max_mm

    r = list(r)
    for i, edge in ((0, 0.0), (1, 0.0), (2, 1.0), (3, 1.0)):
        trial = r.copy()
        trial[i] = edge
        if sensible(trial):
            r = trial
            continue
        good, bad = r[i], edge
        for _ in range(25):
            trial[i] = (good + bad) / 2
            good, bad = (trial[i], bad) if sensible(trial) else (good, trial[i])
        r[i] = good
    return r


def solve(found, paper, image_size, screen_size, paper_mm=(297, 210), box_aspect=0.4, margin=0.04, max_mm=6000.0):
    """found: [(u, v, camera x, camera y)] with u, v as screen fractions. Returns the calibration and details.

    The calibration describes the ground in front of the car in millimetres ("world": x to the car's right, y towards
    the car, (0, 0) = front left corner of the light patch's bounding box) and how it maps onto the projector:
      world_to_screen  3x3 homography world mm -> screen fractions (row major)
      size_mm          width and length of the world: the bounding box of the light patch on the ground
      footprint_mm     the whole light patch on the ground (corners in world mm): the whole screen, extrapolated from
                       the measured squares (capped at max_mm away)
      floor_mm         the part of it where the camera found the squares on the ground
      box_mm           x, y, width, length of the largest box with box_aspect (length / width) inside floor_mm:
                       where the indicator goes
    """
    hc2g, cam_f = paper_homography(paper, paper_mm, image_size)
    w, h = screen_size
    scr = np.float64([[u * w, v * h] for u, v, _, _ in found])
    gnd = cv2.perspectiveTransform(np.float64([[x, y] for _, _, x, y in found]).reshape(-1, 1, 2), hc2g).reshape(-1, 2)
    hs2g, inliers = cv2.findHomography(scr, gnd, cv2.RANSAC, 30.0)
    if hs2g is None:
        raise SystemExit('could not fit the projection: are the squares and the paper visible?')
    inliers = inliers.ravel().astype(bool)
    # Measured floor: the part of the screen whose squares landed on the ground, out to the screen edge where the
    # outermost squares were on the ground too (not where they were missing or on a wall).
    uv = np.array([(u, v) for u, v, _, _ in found])[inliers]
    (u0, v0), (u1, v1) = uv.min(axis=0), uv.max(axis=0)
    u0, v0 = (0.0 if x <= GRID[0] else x for x in (u0, v0))
    u1, v1 = (1.0 if x >= GRID[-1] else x for x in (u1, v1))
    floor_uv = (u0, v0, u1, v1)
    origin, ahead, left = footprint_frame(hs2g, rect_corners(floor_uv, screen_size))
    whole_uv = extend_to_screen(hs2g, floor_uv, screen_size, origin, ahead, max_mm)
    patch = to_ground(hs2g, rect_corners(floor_uv, screen_size))
    whole = to_ground(hs2g, rect_corners(whole_uv, screen_size))

    def local(g):  # x ahead, y left
        return np.column_stack([(g - origin) @ ahead, (g - origin) @ left])

    a_max, l_max = local(whole)[:, 0].max(), local(whole)[:, 1].max()

    def to_world(g):  # x right, y back
        return np.column_stack([l_max - local(g)[:, 1], a_max - local(g)[:, 0]])

    world, floor = to_world(whole), to_world(patch)
    world -= world.min(axis=0)  # the bounding box starts at 0 (it does already, up to rounding)
    size = world.max(axis=0)
    # world -> ground: g = origin + (a_max - y) ahead + (l_max - x) left
    c = origin + a_max * ahead + l_max * left
    w2g = np.array([[-left[0], -ahead[0], c[0]], [-left[1], -ahead[1], c[1]], [0, 0, 1]])
    w2f = np.diag([1 / w, 1 / h, 1]) @ np.linalg.inv(hs2g) @ w2g
    w2f /= w2f[2, 2]
    # The indicator's box (across = world x, along = world y): largest_rectangle works with x ahead, y left, which
    # is (-world y, -world x).
    x0, x1, cy, half = largest_rectangle(np.column_stack([-floor[:, 1], -floor[:, 0]]), aspect=box_aspect)
    half *= 1 - margin
    trim = (x1 - x0) * margin / 2
    x0, x1 = x0 + trim, x1 - trim
    box = [-cy - half, -x1, 2 * half, x1 - x0]
    residual = np.linalg.norm(to_ground(hs2g, scr) - gnd, axis=1)[inliers]

    def r(v):
        return round(float(v), 1)

    cal = {'world_to_screen': [float(f'{v:.9g}') for v in w2f.ravel()], 'size_mm': [r(v) for v in size],
           'footprint_mm': [[r(x), r(y)] for x, y in world], 'floor_mm': [[r(x), r(y)] for x, y in floor],
           'box_mm': [r(v) for v in box], 'screen': list(screen_size)}
    info = {'hc2g': hc2g, 'hs2g': hs2g, 'w2g': w2g, 'inliers': int(inliers.sum()), 'found': len(found),
            'floor_uv': floor_uv, 'whole_uv': whole_uv,
            'residual_mm': float(np.median(residual)) if len(residual) else None, 'camera_f': cam_f,
            'near_mm': float(np.linalg.norm(patch[1] - patch[2])), 'far_mm': float(np.linalg.norm(patch[0] - patch[3]))}
    return cal, info


def plausibility(cal, info, max_mm=8000):
    """None when the result looks like a projector on a floor, else what is wrong with it."""
    floor = np.array(cal['floor_mm'])
    width, length = floor.max(axis=0) - floor.min(axis=0)
    if max(width, length) > max_mm:
        return f"the measured light patch would be {width / 1000:.1f} x {length / 1000:.1f} m"
    if info['inliers'] < 8:
        return f"only {info['inliers']} squares were found on the ground"
    if info['residual_mm'] is not None and info['residual_mm'] > 25:
        return f"the squares fit the ground only within {info['residual_mm']:.0f} mm"
    return None


# ---- measuring ---------------------------------------------------------------------------------------------------

def find_paper(black, white, squares, paper_mm=(297, 210)):
    """The paper: a bright four-cornered shape in the black frame, inside the light patch where the projected squares
    were found, that the camera can see as a flat sheet of paper_mm (so a lit wall edge or a sunny patch on the floor
    is not taken for it). Several brightness levels are tried, as daylight changes how bright the sheet is."""
    gray = cv2.cvtColor(black, cv2.COLOR_BGR2GRAY)
    patch = (cv2.subtract(white, black).max(axis=2) > 60).astype(np.uint8)
    hull = np.zeros_like(patch)
    cv2.fillConvexPoly(hull, cv2.convexHull(np.int32([[x, y] for _, _, x, y in squares])), 1)
    patch = cv2.dilate(patch & hull, np.ones((15, 15), np.uint8))
    inside = gray[patch > 0]
    if inside.size == 0:
        raise SystemExit('the camera does not see the light patch: point it at the projection')
    otsu, _ = cv2.threshold(inside.reshape(1, -1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    levels = sorted({int(otsu), *(int(v) for v in np.percentile(inside, [80, 90, 95, 98]))})
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.01)
    candidates = []
    for level in levels:
        th = ((gray > level) & (patch > 0)).astype(np.uint8) * 255
        for c in cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]:
            area = cv2.contourArea(c)
            if area < 150:
                continue
            q = cv2.approxPolyDP(c, 0.03 * cv2.arcLength(c, True), True)
            if len(q) == 4 and cv2.isContourConvex(q):
                corners = cv2.cornerSubPix(gray, q.reshape(-1, 1, 2).astype(np.float32), (3, 3), (-1, -1), crit)
                candidates.append((area, corners.reshape(-1, 2)))
    for _, corners in sorted(candidates, key=lambda c: -c[0]):
        try:
            paper_homography(corners, paper_mm, gray.shape[::-1])
            return corners
        except SystemExit:
            continue  # not a flat sheet of that size
    raise SystemExit(f'no paper found ({len(candidates)} bright shapes, none looks like a flat {paper_mm[0]:.0f} x '
                     f'{paper_mm[1]:.0f} mm sheet): lay one flat into the light patch, all corners visible to the '
                     'camera, and keep sunlight off the floor around it')


def find_square(frame, black):
    d = cv2.GaussianBlur(cv2.subtract(frame, black).max(axis=2), (5, 5), 0)
    if d.max() < 40:
        return None
    _, th = cv2.threshold(d, int(d.max() * 0.5), 255, cv2.THRESH_BINARY)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(th)
    if n < 2:
        return None
    k = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
    weight = d.astype(np.float64) * (labels == k)
    ys, xs = np.indices(d.shape)
    return float((xs * weight).sum() / weight.sum()), float((ys * weight).sum() / weight.sum())


def shift_px(a, b):
    """How far picture b is shifted against a (pixels); brightness changes of the camera do not count."""
    ga, gb = (cv2.cvtColor(x, cv2.COLOR_BGR2GRAY).astype(np.float32) for x in (a, b))
    (dx, dy), _ = cv2.phaseCorrelate(ga - ga.mean(), gb - gb.mean())
    return float(np.hypot(dx, dy))


class Session:
    """A browser on the projector showing a page, the camera, and the server that tells the page what to show."""

    def __init__(self, screen, camera, page, state, url_query=''):
        """page: path of the page, or its HTML as a string."""
        self.tmp = tempfile.TemporaryDirectory()
        if not isinstance(page, pathlib.Path):
            html, page = page, pathlib.Path(self.tmp.name, 'page.html')
            page.write_text(html, encoding='utf-8')
        self.server = DisplayServer(host='127.0.0.1', port=0, page=page, state=state)
        self.cap = cv2.VideoCapture(camera, cv2.CAP_DSHOW) if hasattr(cv2, 'CAP_DSHOW') else cv2.VideoCapture(camera)
        if not self.cap.isOpened():
            raise SystemExit(f'camera {camera} does not open')
        for _ in range(30):
            self.cap.read()
        browser = find_browser()
        if not browser:
            raise SystemExit('Edge or Chrome is needed')
        cmd = browser_command(browser, f'http://127.0.0.1:{self.server.port}/{url_query}', screen)
        cmd[1] = f'--user-data-dir={pathlib.Path(self.tmp.name, "browser")}'
        self.browser = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(6)

    def show(self, state, settle=0.7):
        self.server.set_state(state)
        time.sleep(settle)

    def grab(self):
        frames = [self.cap.read()[1] for _ in range(6)]
        return np.median(np.stack(frames[-4:]), axis=0).astype(np.uint8)

    def close(self):
        self.cap.release()
        self.browser.terminate()
        self.server.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass  # the browser may still hold its profile for a moment


def measure(screen, camera, out):
    s = Session(screen, camera, PROBE, {'mode': 'black', 'n': 0})
    try:
        n = 0

        def show(**state):
            nonlocal n
            n += 1
            s.show({**state, 'n': n})

        show(mode='black')
        black = s.grab()
        show(mode='white')
        white = s.grab()
        if cv2.subtract(white, black).max() < 60:
            raise SystemExit(f'white and black look the same to the camera: does the projector show screen '
                             f'{screen.index}? (input, cable, Win+P "Extend"), and does the camera see the light patch?')
        found = []
        for v in GRID:
            for u in GRID:
                show(mode='dot', u=u, v=v)
                xy = find_square(s.grab(), black)
                if xy:
                    found.append((u, v, *xy))
        show(mode='black')
        moved = shift_px(black, s.grab())
    finally:
        s.close()
    cv2.imwrite(str(out / 'cal_black.png'), black)
    cv2.imwrite(str(out / 'cal_white.png'), white)
    (out / 'cal_squares.json').write_text(json.dumps(found), encoding='utf-8')  # for looking into it later
    if moved > 1.5:
        raise SystemExit(f'the camera moved by {moved:.1f} px during the measurement: keep it still and measure again')
    return black, white, found


def check(screen, camera, cal, info, out):
    """Projects the calibration grid with the result and saves the camera's top view of it."""
    s = Session(screen, camera, PAGE, {'direction': 'hazard', 'period_ms': 100000, 'live': True, 'failsafe': False},
                url_query=page_query(0, False, True, calibration=cal))
    try:
        frame = s.grab()
    finally:
        s.close()
    def to_g(pts):
        return cv2.perspectiveTransform(np.float64(pts).reshape(-1, 1, 2), info['w2g']).reshape(-1, 2)

    bx, by, bw, bl = cal['box_mm']
    foot, box = to_g(cal['footprint_mm']), to_g([[bx, by], [bx + bw, by], [bx + bw, by + bl], [bx, by + bl]])
    floor = to_g(cal['floor_mm'])
    lo, hi = foot.min(axis=0) - 300, foot.max(axis=0) + 300
    scale = 900 / max(hi - lo)
    t = np.array([[scale, 0, -lo[0] * scale], [0, scale, -lo[1] * scale], [0, 0, 1]])
    top = cv2.warpPerspective(frame, t @ info['hc2g'], tuple(((hi - lo) * scale).astype(int)))
    # whole light patch red, measured floor green, indicator box yellow
    for poly, colour in ((foot, (0, 0, 255)), (floor, (0, 200, 0)), (box, (0, 200, 255))):
        cv2.polylines(top, [((poly - lo) * scale).astype(np.int32).reshape(-1, 1, 2)], True, colour, 1)
    cv2.imwrite(str(out / 'cal_check_camera.png'), frame)
    cv2.imwrite(str(out / 'cal_check_top.png'), top)
    return out / 'cal_check_top.png'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--camera', type=int, default=0, help='camera number')
    p.add_argument('--screen', type=int, help='screen of the projector (default: the first non-primary one)')
    p.add_argument('--paper', default='297x210', help='paper size in mm (default A4)')
    p.add_argument('--out', default=str(CALIBRATION), help='where to save the calibration')
    p.add_argument('--images', default=tempfile.gettempdir(), help='folder for the camera pictures')
    p.add_argument('--no-check', action='store_true', help='do not project and photograph the result')
    args = p.parse_args()

    screen = pick_screen(monitors(), args.screen)
    if screen is None or (screen.primary and args.screen is None):
        raise SystemExit('no projector found: switch it on and set Win+P to "Extend" (or pass --screen)')
    images = pathlib.Path(args.images)
    print(f'projector: screen {screen.index} ({screen.width}x{screen.height}); measuring, about 40 s, keep still ...')
    black, white, found = measure(screen, args.camera, images)
    paper_mm = tuple(sorted((float(v) for v in args.paper.lower().split('x')), reverse=True))
    paper = find_paper(black, white, found, paper_mm)
    (images / 'cal_paper.json').write_text(json.dumps(paper.tolist()), encoding='utf-8')
    cal, info = solve(found, paper, black.shape[1::-1], (screen.width, screen.height), paper_mm)
    print(f"squares: {info['found']} of {len(GRID) ** 2} seen, {info['inliers']} on the ground "
          f"(median error {info['residual_mm']:.0f} mm); camera focal length {info['camera_f'] or 0:.0f} px")
    print(f"light patch on the ground: {info['near_mm']:.0f} mm wide next to the projector, "
          f"{info['far_mm']:.0f} mm at the far end of the measured part")
    whole = info['whole_uv']
    print(f"ground in use: {cal['size_mm'][0]:.0f} mm wide x {cal['size_mm'][1]:.0f} mm long (bounding box of the "
          f"light patch), indicator box {cal['box_mm'][2]:.0f} x {cal['box_mm'][3]:.0f} mm")
    print(f"screen used: {(whole[2] - whole[0]) * (whole[3] - whole[1]) * 100:.0f} % "
          f"(measured on the floor: u {info['floor_uv'][0]:.2f}..{info['floor_uv'][2]:.2f}, "
          f"v {info['floor_uv'][1]:.2f}..{info['floor_uv'][3]:.2f}; the rest is extrapolated)")
    problem = plausibility(cal, info)
    if problem:
        raise SystemExit(f'not saved, the measurement looks wrong: {problem}. Check the paper (flat, all corners '
                         'visible, nothing bright next to it), keep the room light steady and measure again.')
    pathlib.Path(args.out).write_text(json.dumps(cal, indent=2) + '\n', encoding='utf-8')
    print(f'saved {args.out}; projector_indicator.indicator uses it from now on')
    if not args.no_check:
        print(f'check: top view of the projected grid saved to {check(screen, args.camera, cal, info, images)}')


if __name__ == '__main__':
    main()
