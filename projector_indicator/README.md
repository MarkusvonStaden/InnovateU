# Projector indicator

Projects onto the ground in front of the car, from a laptop on the car's network with a projector as its second
screen: the turn signal, the path the car is about to drive, red dots under the people the person detection sees and
an advertisement card with a navigation hint. Everything lands undistorted on the light patch thanks to a camera
calibration (an A4 sheet as the ruler).

It only listens to the car's foxglove_bridge (`/lights_state`, `/steer_state`, `/persons`, `/advertisement`) and
never sends anything. No ROS needed on the laptop.

## Setup

```bash
cd projector_indicator
uv sync                      # or: python -m venv .venv && .venv/Scripts/pip install -e . pytest
uv run pytest                # tests, no car needed
```

Windows: Edge or Chrome opens as a kiosk window on the projector; set the projector to "Extend" (Win+P).

## Running it

A projector on the laptop's second screen shows on the ground in front of the car which way BITT is indicating:
amber chevrons that light up from the inside out, on the left, the right or both (hazard). Nothing else is projected:
when the indicator is off, or the car has not been heard from for 3 s, the picture is black.

```bash
# laptop on the car's network, projector connected and set to "Extend" (Win+P)
uv run python -m projector_indicator.indicator                        # opens full screen on the projector (Alt+F4 closes it)
uv run python -m projector_indicator.indicator --calibrate            # FRONT / LEFT / RIGHT picture to set up the projector
uv run python -m projector_indicator.indicator --rotate 270 --no-mirror --keystone 0.4   # see "Setting up" below
uv run python -m projector_indicator.indicator --list-screens         # which screen is which; --screen 1 to choose
uv run python -m projector_indicator.indicator --topic /lights_cmd    # if the car does not publish /lights_state
uv run python -m projector_indicator.indicator --url ws://172.16.39.142:8765   # another bridge (also foxglove_bridge 3.x)
```

- It also shows the message of `/advertisement` (`std_msgs/String`) as a card right between the chevrons (the
  focus of the projection), which shrink to the edges meanwhile: a navigation banner with a turn arrow and a
  distance over the destination with its logo. A message naming a known brand gets its card (`Porsche` -> Porsche
  Zentrum, right, 100 m; `McDonald's` -> left, 250 m; more in [ads.py](projector_indicator/ads.py)); any other text becomes
  the destination with a made-up direction and distance. An empty message clears it. Logos: put the brand's
  official logo file into `projector_indicator/logos/` (`porsche.svg`, `mcdonalds.svg`, ...; git-ignored, logos are the
  brands' trademarks); without one the card shows a plain badge in the brand's colours. `--text-topic` picks another
  topic ('' for none), `--text "..."` shows a card until the first message comes (and in `--demo`), `--text-s 10`
  hides a message after 10 s. With the fake car: type `a Porsche`.
- Persons walking into the car's way (`/persons`, `std_msgs/String` with JSON from the person detection: `x`, `y`
  in base_link metres, `zone` ahead / left / right) get a red dot on the ground under them (28 cm, soft rim). Each
  person is followed from message to message and smoothed (time constant 0.35 s), so the dot glides along; when the
  detection no longer reports them it fades out over 1 s (`--persons-s`). Outside the light patch it moves just
  inside. The advertisement stays meanwhile. Where the light patch is relative to the car has to be
  told: `--light-start-m` = metres from the rear axle to the middle of the light's edge next to the car (default
  1.05, the front bumper; measure it on the car), `--light-offset-m` if that point is not on the car's centre line.
  With the fake car: type `p 2.5 -0.3` (a person at x = 2.5 m, y = -0.3 m for 3 s).
- **The car's path** (`/steer_state`): the corridor the car is about to drive is drawn on the ground, from the steering
  angle with the bicycle model (wheelbase 0.72 m, as wide as the car, 0.76 m; [ground.py](projector_indicator/ground.py)).
  White while free, red when a person is in it. Only while steering data comes in. The steering angle is smoothed
  (`STEER_TAU_S`), and the path fades out towards the edge of the light patch. With the fake car: `s 0.3` steers.
- Starting it again replaces the running one on the same port (it is asked to stop and closes its projector
  window), so a restart never leaves the old page on the projector.
- It only listens and never sends, so it runs next to everything else. It reads `/lights_state`, which the firmware
  publishes with what it really shows, so indicators from Foxglove, `controls.py`, your own code or the failsafe all
  show up.
- The projection blinks at the car's `blink_period_ms`, but not in phase with the lamps: `/lights_state` says
  *that* the car is indicating, not whether the lamp is on right now.
- If the car only publishes `/lights_state` on changes, the projection would go dark after 3 s: use `--stale-s 0`.
- How: [indicator.py](projector_indicator/indicator.py) (bridge connection, decides what to show),
  [indicator.html](projector_indicator/indicator.html) (the picture), [screens.py](projector_indicator/screens.py) (finds the
  projector and opens Edge or Chrome as a kiosk window on it).

## Setting up the projector: with a camera (recommended)

The projector shines onto the ground at a slant, so a plain picture comes out as a strongly stretched trapezoid
(measured: about 0.34 m wide next to the projector, 1 m wide 1.4 m further on). The camera calibration measures that
and turns the whole light patch into an undistorted drawing surface in millimetres:

```bash
uv run python -m projector_indicator.projector_cal          # about 45 s; then projector_indicator.indicator uses the result by itself
```

Before: projector on (Win+P "Extend"), room dark, a camera (the laptop's webcam is fine) that sees the whole light
patch from as high as possible, **one A4 sheet flat on the ground inside the light patch** (its corners visible), and
nobody moving. It projects black, white and 25 single squares, finds them with the camera, uses the paper as the
ruler (a camera model with one focal length, so it extrapolates sensibly beyond the sheet) and fits the projector ->
ground mapping. That mapping is then used for the **whole** projector picture: where the camera found no squares on
the floor (out of its view, or on a wall) it is extrapolated, capped 6 m away. Squares landing on a wall are left out
of the fit; the indicator stays where squares were measured on the floor. Daylight or a lamp shining onto the floor
can make it misread the paper: it then stops, or refuses to save an implausible result. It saves to
`projector_indicator/projector_local.json` (git-ignored):

| | |
|---|---|
| `world_to_screen` | ground mm -> screen: the page uses it as a CSS `matrix3d`, so everything it draws lands undistorted |
| `size_mm` | the drawing surface: the bounding box of the light patch on the ground |
| `footprint_mm` | the whole light patch (4 corners): only inside it can anything be seen |
| `floor_mm` | the part of it where the camera measured squares on the floor |
| `box_mm` | the largest box for the indicator inside `floor_mm` (the light patch is narrow next to the car, wide ahead) |

The drawing surface on the page (`#world`): 1 px = 1 mm on the ground, x to the car's right, y towards the car,
(0, 0) = front left corner of the bounding box. Own content such as a logo goes into `#ground` in
[indicator.html](projector_indicator/indicator.html), positioned and sized in millimetres; it comes out undistorted wherever
it lies inside `footprint_mm` (`--calibrate` draws that outline and a 10 cm grid).

Afterwards it photographs the result: the top view `cal_check_top.png` (in the temp folder, or `--images`) shows the
projected grid with the whole light patch in red, the measured floor in green and the indicator box in yellow. Measure again whenever the projector or
its mounting moves. `projector_indicator.indicator --no-calibration` ignores the file. How:
[projector_cal.py](projector_indicator/projector_cal.py).

## Setting up the projector: by hand

The page is drawn in the car's frame (up = ahead of the car, left = the car's left), then fitted to the projector as a
whole. Without a camera calibration these settings apply:

- `--rotate 90`: the projector is turned by 90 degrees.
- `--mirror` (on; `--no-mirror` switches it off): without it the picture came out mirror-inverted.
- `--keystone 0.5`: the projector shines onto the ground at a slant, so its far edge comes out much wider
  than the near one. The page draws the front edge narrower by this fraction (and the far rows closer together), so
  the slant turns it back into a rectangle. 0 = no correction, negative = narrow the edge next to the car instead.

Start with `--calibrate`: it shows a grid of squares. Then on the page (click into it first):

| Key | |
|---|---|
| `R` | turn by another 90 degrees, until FRONT points away from the car |
| `M` | mirror, until LEFT is on the car's left and the text reads correctly |
| `↑` / `↓` | more / less keystone (with Shift: faster), until the frame on the ground is a rectangle and the squares look square |
| `C` | calibration picture on/off |
| `F` | full screen on/off |

The settings stay in the page address while it is open. Note `rotate`, `mirror` and `keystone` shown on the page
and pass them next time as `--rotate`, `--mirror` / `--no-mirror` and `--keystone`. Switch the projector's own
keystone correction off (or to 0), otherwise both correct at once.

## Without the car: fake car or demo

```bash
uv run python -m projector_indicator.indicator --demo                 # no network: left, right, hazard, off, one after another

uv run python -m projector_indicator.fake_car                         # terminal 1: a pretend car; type l, r, h, o + Enter
uv run python -m projector_indicator.indicator --url ws://localhost:8765   # terminal 2: the projection follows it
uv run python -m projector_indicator.fake_car --cycle 3               # or let it change by itself every 3 s
```

[fake_car.py](projector_indicator/fake_car.py) is a tiny Foxglove bridge that only knows the lights: it publishes
`/lights_state` and, like the ESP32, takes `/lights_cmd`. So you can also connect the Foxglove app to
`ws://localhost:8765` and use the light buttons of the layout.

