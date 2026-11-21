"""People in one frame with an off-the-shelf YOLO model (COCO class 0 = person)."""


class Detector:
    def __init__(self, model="yolo11n.pt", min_conf=0.4):
        from ultralytics import YOLO  # imported here so geometry and its tests run without torch

        self.model = YOLO(model)  # downloads the weights on first use
        self.min_conf = min_conf

    def __call__(self, frame):
        """frame: HxWx3 BGR array. Returns [((x1, y1, x2, y2), confidence), ...] in pixels."""
        result = self.model(frame, conf=self.min_conf, classes=[0], verbose=False)[0]
        return list(zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist()))
