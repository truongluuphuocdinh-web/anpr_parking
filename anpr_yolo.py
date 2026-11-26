"""Bộ nhận diện biển số dùng 2 model YOLO (kiểu các repo GitHub biển số VN):
  - LP_detector.pt : dò khung biển số trong ảnh
  - LP_ocr.pt      : dò TỪNG KÝ TỰ trong ảnh biển rồi ghép lại (hỗ trợ biển 1 dòng và 2 dòng)

Hai class dưới đây có CÙNG giao diện với PlateDetector / PlateOCR cũ trong anpr.py:
    YoloPlateDetector.detect(frame) -> (crop, box, conf) | None
    YoloCharOCR.read(crop)          -> (text, conf)
nên AnprPipeline, app.py, services.py không phải sửa gì.

Nạp được cả trọng số YOLOv5 gốc (qua gói `yolov5`) lẫn YOLOv8 (qua `ultralytics`).
"""
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

import config
from plate_utils import is_valid, normalize


@dataclass
class Det:
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    label: str


class YoloModel:
    """Bọc một file .pt YOLO (v5 hoặc v8) thành hàm predict(img, conf) -> list[Det]."""

    def __init__(self, path, size=640):
        self.size, self.kind, self.model = size, None, None
        errors = []
        try:                                    # YOLOv5 gốc (LP_detector.pt, LP_ocr.pt của các repo GitHub)
            import yolov5
            self.model, self.kind = yolov5.load(str(path)), "yolov5"
        except Exception as e:
            errors.append(f"yolov5: {e.__class__.__name__}: {e}")
        if self.model is None:
            try:                                # YOLOv8 / yolov5u
                from ultralytics import YOLO
                self.model, self.kind = YOLO(str(path)), "ultralytics"
            except Exception as e:
                errors.append(f"ultralytics: {e.__class__.__name__}: {e}")
        if self.model is None:
            raise RuntimeError(f"Không nạp được {path}. " + " | ".join(errors))

    def predict(self, img, conf=0.25):
        out = []
        if self.kind == "yolov5":
            self.model.conf = conf
            rows = self.model(img[:, :, ::-1], size=self.size).xyxy[0].tolist()     # yolov5 nhận RGB
            names = self.model.names
            for x1, y1, x2, y2, c, k in rows:
                out.append(Det(x1, y1, x2, y2, c, str(names[int(k)])))
        else:
            res = self.model(img, verbose=False, conf=conf)[0]
            for (x1, y1, x2, y2), c, k in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(),
                                              res.boxes.cls.tolist()):
                out.append(Det(x1, y1, x2, y2, c, str(res.names[int(k)])))
        return out


def _iou(a: Det, b: Det) -> float:
    iw = min(a.x2, b.x2) - max(a.x1, b.x1)
    ih = min(a.y2, b.y2) - max(a.y1, b.y1)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / ((a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter)


class YoloPlateDetector:
    """Dò biển số bằng YOLO. Cùng giao diện với PlateDetector cũ."""

    def __init__(self, model=None, path=config.LP_DETECTOR_PATH, conf=config.DETECT_CONF):
        self.model = model or YoloModel(path)
        self.conf, self.mode = conf, "yolo"

    def detect(self, frame):
        dets = self.model.predict(frame, conf=self.conf)
        if not dets:
            return None
        d = max(dets, key=lambda d: d.conf)
        h, w = frame.shape[:2]
        bw, bh = d.x2 - d.x1, d.y2 - d.y1
        px, py = int(bw * 0.04), int(bh * 0.06)                   # chừa lề nhỏ để không cắt mất chữ
        x1, y1 = max(0, int(d.x1) - px), max(0, int(d.y1) - py)
        x2, y2 = min(w, int(d.x2) + px), min(h, int(d.y2) + py)
        if x2 - x1 < 20 or y2 - y1 < 10:
            return None
        return frame[y1:y2, x1:x2].copy(), (x1, y1, x2, y2), float(d.conf)


class YoloCharOCR:
    """Đọc biển bằng cách dò từng ký tự rồi sắp xếp theo vị trí. Cùng giao diện với PlateOCR cũ."""

    def __init__(self, model=None, path=config.LP_OCR_PATH, conf=0.25):
        self.model = model or YoloModel(path)
        self.conf = conf
        self.engine, self.backend, self.error, self.last_debug = self.model, "yolo-char", "", []

    # ---- ghép ký tự thành chuỗi ----
    @staticmethod
    def dedupe(dets, thr=0.6):
        """Bỏ ký tự bị dò trùng chồng lên nhau (giữ cái tin cậy hơn)."""
        keep = []
        for d in sorted(dets, key=lambda d: -d.conf):
            if all(_iou(d, k) < thr for k in keep):
                keep.append(d)
        return keep

    @staticmethod
    def arrange(dets):
        """Sắp ký tự: biển 1 dòng -> trái sang phải; biển 2 dòng -> dòng trên rồi dòng dưới."""
        cy = lambda d: (d.y1 + d.y2) / 2
        cx = lambda d: (d.x1 + d.x2) / 2
        hmed = float(np.median([d.y2 - d.y1 for d in dets]))
        ys = [cy(d) for d in dets]
        if max(ys) - min(ys) > 0.6 * hmed:                         # lệch chiều dọc đủ lớn -> 2 dòng
            mid = (max(ys) + min(ys)) / 2
            lines = [[d for d in dets if cy(d) < mid], [d for d in dets if cy(d) >= mid]]
        else:
            lines = [list(dets)]
        return [d for ln in lines for d in sorted(ln, key=cx)]

    def _read_once(self, img):
        dets = self.dedupe(self.model.predict(img, conf=self.conf))
        if not dets:
            return "", 0.0
        ordered = self.arrange(dets)
        text = "".join(d.label for d in ordered)
        return text, float(np.mean([d.conf for d in ordered]))

    def read(self, crop):
        """Trả về (chuỗi thô, độ tin cậy). Nếu lần đọc đầu chưa đúng định dạng, thử lại sau khi chỉnh thẳng biển."""
        self.last_debug = []
        text, conf = self._read_once(crop)
        self.last_debug.append((0, "orig", text, conf, crop))
        if is_valid(normalize(text)):
            return text, conf
        try:
            from anpr import PlateOCR                              # dùng lại hàm deskew có sẵn
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
            straight = cv2.cvtColor(PlateOCR.deskew(gray), cv2.COLOR_GRAY2BGR)
            t2, c2 = self._read_once(straight)
            self.last_debug.append((0, "deskew", t2, c2, straight))
            if is_valid(normalize(t2)) or c2 > conf:
                return t2, c2
        except Exception:
            pass
        return text, conf


def yolo_available() -> bool:
    return Path(config.LP_DETECTOR_PATH).exists() and Path(config.LP_OCR_PATH).exists()
