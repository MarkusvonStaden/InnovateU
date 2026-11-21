"""Person Finder: where do people stand in front of the car? Front camera -> YOLO -> position on the floor.

Runs on a laptop without ROS, talks to the car (or the fake BITT / Gazebo sim) through its foxglove_bridge:

    uv run person-finder                                   # real car, ws://10.42.0.1:8765
    uv run person-finder --bridge ws://localhost:8765      # fake BITT with a bag, see README

Subscribes:  /preview/left/compressed (car, bags) or /visual_slam/image_0 (Gazebo), /visual_slam/camera_info_0
Publishes:   /persons                   std_msgs/String, JSON: {"stamp", "frame_id", "persons": [...]}
             /persons/image/compressed  sensor_msgs/CompressedImage, the camera picture with the boxes drawn in
"""
import argparse
import json
import os
import threading
import time
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv

from person_finder.cdr import Codec, normalize
from person_finder.foxglove_client import FoxgloveClient
from person_finder.geometry import DEFAULT_K, Camera, locate

# Preferred first. Over Wi-Fi the small JPEG preview (~5 fps); on this computer (fake BITT, Gazebo) the raw
# 60 fps camera, which is far too big for Wi-Fi (~13 MB/s) but makes the picture smooth.
CAMERA_TOPICS = ["/preview/left/compressed", "/visual_slam/image_0"]
LOCAL_CAMERA_TOPICS = ["/visual_slam/image_0", "/preview/left/compressed"]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def camera_topics(bridge_url):
    host = urlparse(bridge_url).hostname or ""
    return LOCAL_CAMERA_TOPICS if host in LOCAL_HOSTS else CAMERA_TOPICS
ZONE_DE = {"ahead": "vor dem Auto", "left": "links", "right": "rechts"}


class Latest:
    """The newest frame only: when detection is slower than the camera, old frames are skipped, not queued."""

    def __init__(self):
        self._item, self._cond = None, threading.Condition()

    def put(self, item):
        with self._cond:
            self._item = item
            self._cond.notify()

    def take(self, timeout):
        with self._cond:
            if self._item is None:
                self._cond.wait(timeout)
            item, self._item = self._item, None
            return item


def to_bgr(schema, msg):
    """Decoded CompressedImage / Image -> HxWx3 BGR array (None for encodings we do not know)."""
    import cv2
    import numpy as np

    if schema == "sensor_msgs/CompressedImage":
        return cv2.imdecode(np.frombuffer(msg["data"], np.uint8), cv2.IMREAD_COLOR)
    h, w, enc = msg["height"], msg["width"], msg["encoding"]
    raw = np.frombuffer(msg["data"], np.uint8)
    channels = {"mono8": 1, "8UC1": 1, "rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4}.get(enc)
    if channels is None:
        return None
    img = raw.reshape(h, msg["step"])[:, : w * channels].reshape(h, w, channels)
    conversion = cv2.COLOR_GRAY2BGR if channels == 1 else {
        "rgb8": cv2.COLOR_RGB2BGR, "rgba8": cv2.COLOR_RGBA2BGR, "bgra8": cv2.COLOR_BGRA2BGR}.get(enc)
    return cv2.cvtColor(img, conversion) if conversion is not None else img.copy()


def draw(img, persons):
    import cv2

    for p in persons:
        x1, y1, x2, y2 = (int(v) for v in p.box)
        color = (0, 0, 255) if p.zone == "ahead" else (0, 200, 255)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        label = f"{'<' if p.method == 'edge' else ''}{p.distance:.1f} m {ZONE_DE[p.zone]}"
        cv2.putText(img, label, (x1, max(y1 - 6, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2, cv2.LINE_AA)
    return img


def describe(p):
    near = "höchstens " if p.method == "edge" else ""
    return f"{near}{p.distance:.1f} m, {ZONE_DE[p.zone]} ({p.y:+.1f} m seitlich, {p.angle_deg:+.0f}°)"


class PersonFinder:
    def __init__(self, args):
        self.args = args
        self.codec = Codec()
        self.frames = Latest()
        self.calibration = None  # Camera from CameraInfo, at the calibration's own resolution
        self.camera_topic = None
        self.client = FoxgloveClient(args.bridge, topics=[args.camera_info], on_message=self.on_message)
        # Results go to the same bridge, or with --publish-bridge to another one (e.g. a bridge on this laptop when
        # the car's bridge does not pass on what laptops publish).
        self.out = self.client
        if args.publish_bridge and args.publish_bridge != args.bridge:
            self.out = FoxgloveClient(args.publish_bridge, topics=[], on_message=lambda *_: None)
        self.last_print, self.last_summary = 0.0, None

    # ---- bridge thread ----------------------------------------------------------------------------------------

    def on_message(self, topic, payload, channel):
        schema = normalize(channel.get("schemaName", ""))
        if topic == self.args.camera_info:
            info = self.codec.decode("sensor_msgs/CameraInfo", payload)
            k = info["k"]
            if info["width"] and k[0]:
                self.calibration = Camera(k[0], k[4], k[2], k[5], info["width"], info["height"])
        elif topic == self.camera_topic:
            self.frames.put((schema, payload, time.monotonic()))

    # ---- main thread ------------------------------------------------------------------------------------------

    def pick_camera(self):
        if self.args.topic:
            return self.args.topic
        print("📷 waiting for the camera topics …")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.client.connected.wait(1):
                time.sleep(1.0)  # advertisements arrive right after serverInfo
                topics = set(self.client.advertised_topics())
                for t in camera_topics(self.args.bridge):
                    if t in topics:
                        return t
        raise SystemExit(f"none of {CAMERA_TOPICS} is advertised by {self.args.bridge}; pick one with --topic")

    def run(self):
        from person_finder.detector import Detector

        args = self.args
        detector = Detector(args.model, args.conf)  # loads before connecting: first run downloads the weights
        self.client.start()
        if self.out is not self.client:
            self.out.start()
        if args.publish:
            self.out.advertise(args.out_topic, "std_msgs/msg/String")
            if args.image_topic:
                self.out.advertise(args.image_topic, "sensor_msgs/msg/CompressedImage")
        self.camera_topic = self.pick_camera()
        self.client.subscribe([self.camera_topic])
        print(f"📷 {self.camera_topic} -> persons" + (f" -> {args.out_topic}" if args.publish else "")
              + (f" on {args.publish_bridge}" if self.out is not self.client else ""))

        min_dt = 1.0 / args.max_hz if args.max_hz else 0.0
        last = 0.0
        try:
            while True:
                item = self.frames.take(timeout=1.0)
                if item is None:
                    continue
                if time.monotonic() - last < min_dt:
                    continue
                last = time.monotonic()
                self.process(detector, *item)
        except KeyboardInterrupt:
            pass
        finally:
            self.client.close()
            if self.out is not self.client:
                self.out.close()

    def process(self, detector, schema, payload, received):
        import cv2

        msg = self.codec.decode(schema, payload)
        img = to_bgr(schema, msg)
        if img is None:
            print(f"⚠️  cannot read image encoding {msg.get('encoding')!r}")
            return
        h, w = img.shape[:2]
        base = self.calibration or Camera(**DEFAULT_K)
        cam = base.scaled(w, h)
        persons = [locate(box, cam, conf, self.args.person_height) for box, conf in detector(img)]
        persons.sort(key=lambda p: p.distance)
        latency_ms = (time.monotonic() - received) * 1000

        self.report(persons, latency_ms)
        if self.args.publish:
            header = msg["header"]
            data = {"stamp": header["stamp"]["sec"] + header["stamp"]["nanosec"] * 1e-9,
                    "frame_id": "base_link", "persons": [p.to_dict() for p in persons]}
            self.out.publish(self.args.out_topic, self.codec.encode(
                "std_msgs/String", {"data": json.dumps(data)}))
            if self.args.image_topic:
                ok, jpeg = cv2.imencode(".jpg", draw(img, persons), [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    self.out.publish(self.args.image_topic, self.codec.encode(
                        "sensor_msgs/CompressedImage", {"header": header, "format": "jpeg", "data": jpeg.tobytes()}))
        if self.args.show:
            cv2.imshow("Person Finder", draw(img, persons))
            if cv2.waitKey(1) == 27:  # Esc
                raise KeyboardInterrupt

    def report(self, persons, latency_ms):
        """Print when what we see changes, and otherwise once a second while someone is there."""
        summary = [(p.zone, round(p.distance)) for p in persons]
        now = time.monotonic()
        if summary == self.last_summary and (not persons or now - self.last_print < 1.0):
            return
        self.last_summary, self.last_print = summary, now
        if not persons:
            print("🚶 keine Person")
            return
        print(f"🚶 {len(persons)} Person{'en' if len(persons) > 1 else ''} ({latency_ms:.0f} ms)")
        for p in persons:
            print(f"   - {describe(p)}")


def main():
    load_dotenv(find_dotenv(usecwd=True))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bridge", default=os.environ.get("BITT_BRIDGE", "ws://10.42.0.1:8765"),
                        help="foxglove_bridge of the car (env BITT_BRIDGE)")
    parser.add_argument("--publish-bridge", default=os.environ.get("PERSON_PUBLISH_BRIDGE", ""),
                        help="publish the results to this bridge instead (default: the same as --bridge)")
    parser.add_argument("--topic", help="camera topic (default: /visual_slam/image_0 for a bridge on localhost, "
                        "otherwise /preview/left/compressed)")
    parser.add_argument("--camera-info", default="/visual_slam/camera_info_0")
    parser.add_argument("--model", default=os.environ.get("PERSON_MODEL", "yolo11n.pt"), help="YOLO weights")
    parser.add_argument("--conf", type=float, default=0.4, help="minimum detector confidence")
    parser.add_argument("--person-height", type=float, default=1.70, help="assumed body height in m")
    parser.add_argument("--max-hz", type=float, default=20.0, help="at most this many frames per second (0 = all)")
    parser.add_argument("--out-topic", default="/persons")
    parser.add_argument("--image-topic", default="/persons/image/compressed", help="'' to not publish the picture")
    parser.add_argument("--no-publish", dest="publish", action="store_false", help="only print, publish nothing")
    parser.add_argument("--show", action="store_true", help="also show the picture in a window (Esc quits)")
    PersonFinder(parser.parse_args()).run()


if __name__ == "__main__":
    main()
