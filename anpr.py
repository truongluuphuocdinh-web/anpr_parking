"""Module AI: camera/ảnh -> YOLO phát hiện biển -> OCR -> chuẩn hóa -> PlateResult."""
import itertools
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

import config
from plate_utils import is_valid, normalize, pretty, to_key

_INFER_LOCK = threading.Lock()      # luồng nhận diện trực tiếp và nút QUÉT dùng chung model -> không chạy song song


@dataclass
class PlateResult:
    text: str = ""
    key: str = ""
    det_conf: float = 0.0
    ocr_conf: float = 0.0
    crop: Optional[np.ndarray] = None
    box: Optional[tuple] = None
    valid: bool = False
    message: str = ""


class PlateDetector:
    """Dùng YOLO nếu có models/plate.pt; nếu không, dùng dò khung chữ nhật bằng OpenCV (chỉ để thử nghiệm)."""

    def __init__(self, model_path=config.YOLO_MODEL_PATH, conf=config.DETECT_CONF):
        self.conf, self.model, self.mode = conf, None, "contour"
        if Path(model_path).exists():
            try:
                from ultralytics import YOLO
                self.model, self.mode = YOLO(str(model_path)), "yolo"
            except Exception as e:                      # thiếu thư viện / file hỏng
                print("Không nạp được YOLO, dùng bộ dò dự phòng:", e)

    def detect(self, frame):
        """Trả về (crop, box(x1,y1,x2,y2), conf) hoặc None."""
        box = conf = None
        if self.model is not None:
            res = self.model(frame, verbose=False, conf=self.conf)[0]
            if len(res.boxes):
                i = int(res.boxes.conf.argmax())
                box = tuple(int(v) for v in res.boxes.xyxy[i].tolist())
                conf = float(res.boxes.conf[i])
        else:
            box, conf = self._contour_detect(frame), 0.5
        if box is None:
            return None
        h, w = frame.shape[:2]
        px, py = int((box[2] - box[0]) * 0.04), int((box[3] - box[1]) * 0.06)
        x1, y1, x2, y2 = max(0, box[0] - px), max(0, box[1] - py), min(w, box[2] + px), min(h, box[3] + py)
        if x2 - x1 < 20 or y2 - y1 < 10:
            return None
        return frame[y1:y2, x1:x2].copy(), (x1, y1, x2, y2), conf

    @staticmethod
    def _contour_detect(frame):
        h, w = frame.shape[:2]
        gray = cv2.bilateralFilter(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 11, 17, 17)
        edges = cv2.dilate(cv2.Canny(gray, 30, 200), np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:40]:
            x, y, bw, bh = cv2.boundingRect(c)
            area = bw * bh
            if not (0.01 * w * h <= area <= 0.85 * w * h):
                continue
            if not (1.1 <= bw / bh <= 5.5):                       # biển xe máy ~1.3, ô tô ~4.3
                continue
            if cv2.contourArea(c) / area < 0.5:
                continue
            return (x, y, x + bw, y + bh)
        return None


class PlateOCR:
    """OCR chuyên cho biển số:
    - Chỉ dùng bước *nhận dạng* (bỏ bước dò chữ vốn hay sai trên biển nhỏ).
    - Biển 2 dòng (xe máy) được tách đôi, đọc riêng từng dòng.
    - Mỗi dòng thử nhiều biến thể ảnh (CLAHE, nhị phân Otsu, làm mờ nhẹ...), rồi chọn tổ hợp
      đúng định dạng biển số nhất. Hỗ trợ RapidOCR (ưu tiên) và EasyOCR (dự phòng)."""

    ALLOW = "ABCDEFGHKLMNPRSTUVXYZ0123456789"
    LINE_HEIGHT = 96

    def __init__(self, backend=config.OCR_BACKEND):
        self.engine, self.backend, self.error = None, "", ""
        self.last_debug = []
        errors = []
        for name in (["rapidocr", "easyocr"] if backend == "auto" else [backend]):
            try:
                if name == "rapidocr":
                    from rapidocr_onnxruntime import RapidOCR
                    self.engine = RapidOCR()
                else:
                    import easyocr
                    self.engine = easyocr.Reader(["en"], gpu=False, verbose=True)
                self.backend = name
                break
            except Exception as e:
                errors.append(f"{name}: {e.__class__.__name__}")
        if self.engine is None:
            self.error = ("OCR chưa sẵn sàng (" + "; ".join(errors) +
                          "). Hãy chạy: pip install rapidocr-onnxruntime")

    # ---- tiền xử lý ----
    @staticmethod
    def split_lines(gray):
        """Biển gần vuông (h/w > 0.5) là biển 2 dòng -> tách đôi, chồng nhẹ để không cắt mất chữ."""
        h, w = gray.shape
        if h / w > 0.5:
            mid, ov = h // 2, max(1, int(h * 0.04))
            return [gray[:mid + ov], gray[mid - ov:]]
        return [gray]

    @staticmethod
    def deskew(gray):
        """Chỉnh thẳng biển bị nghiêng (±12°): chọn góc làm các dòng chữ 'sắc nét' nhất trên biểu đồ chiếu ngang."""
        g = 255 - gray if gray.mean() < 110 else gray
        s = 160 / max(g.shape)
        small = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else g
        _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        h, w = bw.shape
        best_ang, best_score = 0.0, -1.0
        for ang in np.arange(-12, 12.1, 1.5):
            M = cv2.getRotationMatrix2D((w / 2, h / 2), float(ang), 1)
            score = float(np.var(cv2.warpAffine(bw, M, (w, h)).sum(axis=1)))
            if score > best_score:
                best_ang, best_score = float(ang), score
        if abs(best_ang) < 1:
            return gray
        H, W = gray.shape
        M = cv2.getRotationMatrix2D((W / 2, H / 2), best_ang, 1)
        return cv2.warpAffine(gray, M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    @staticmethod
    def tighten(line):
        """Thu gọn khung quanh vùng ký tự (bỏ nền, viền biển) để chữ chiếm đủ lớn trước khi OCR."""
        g = 255 - line if line.mean() < 110 else line
        _, bw = cv2.threshold(cv2.GaussianBlur(g, (3, 3), 0), 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        H, W = bw.shape
        n, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        boxes = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            touches = x <= 1 or y <= 1 or x + w >= W - 1 or y + h >= H - 1
            if not touches and 0.18 * H <= h <= 0.98 * H and w <= 0.5 * W and area > 0.2 * w * h * 0.3:
                boxes.append((x, y, w, h))
        if len(boxes) < 3:
            return line
        x1 = min(b[0] for b in boxes); y1 = min(b[1] for b in boxes)
        x2 = max(b[0] + b[2] for b in boxes); y2 = max(b[1] + b[3] for b in boxes)
        m = int(0.5 * float(np.median([b[3] for b in boxes])))
        return line[max(0, y1 - m):min(H, y2 + m), max(0, x1 - m):min(W, x2 + m)]

    def variants(self, line):
        g = line
        if g.mean() < 110:                                  # biển nền tối -> đảo thành chữ đen nền sáng
            g = 255 - g
        h = g.shape[0]
        s = self.LINE_HEIGHT / h
        g = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)
        g = cv2.copyMakeBorder(g, 12, 12, 16, 16, cv2.BORDER_REPLICATE)
        blur = cv2.GaussianBlur(g, (3, 3), 0)               # giảm vân moiré khi quay màn hình
        _, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        clahe = cv2.createCLAHE(2.0, (8, 8)).apply(g)
        return [("clahe", clahe), ("gray", g), ("otsu", otsu), ("blur", blur)]

    # ---- nhận dạng một dòng ----
    def _rec(self, gray_line):
        if self.backend == "rapidocr":
            res, _ = self.engine(cv2.cvtColor(gray_line, cv2.COLOR_GRAY2BGR),
                                 use_det=False, use_cls=False, use_rec=True)
            return (res[0][0], float(res[0][1])) if res else ("", 0.0)
        out = self.engine.recognize(gray_line, allowlist=self.ALLOW, detail=1, paragraph=False)
        return (out[0][1], float(out[0][2])) if out else ("", 0.0)

    def read(self, crop):
        """Trả về (chuỗi thô, độ tin cậy). Lưu chi tiết từng biến thể vào self.last_debug."""
        if self.engine is None:
            raise RuntimeError(self.error)
        self.last_debug = []
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        h, w = gray.shape
        ty, tx = int(h * 0.04), int(w * 0.04)               # cắt bỏ viền khung biển
        gray = self.deskew(gray[ty:h - ty, tx:w - tx])

        per_line = []
        for li, line in enumerate(self.split_lines(gray)):
            cands = {}                                       # text -> [tổng conf, conf lớn nhất]
            for name, img in self.variants(self.tighten(line)):
                raw, conf = self._rec(img)
                self.last_debug.append((li, name, raw, conf, img))
                t = to_key(raw)
                if t:
                    c = cands.setdefault(t, [0.0, 0.0])
                    c[0] += conf
                    c[1] = max(c[1], conf)
            ranked = sorted(cands.items(), key=lambda kv: -kv[1][0])[:3]     # các biến thể trùng nhau = "bỏ phiếu"
            per_line.append([(t, c[0], c[1]) for t, c in ranked])   # (chữ, tổng phiếu, conf lớn nhất)
        if not per_line or any(not c for c in per_line):
            return "", 0.0

        best = None
        for combo in itertools.product(*per_line):          # ưu tiên tổ hợp đúng định dạng biển số
            text = "".join(t for t, _, _ in combo)
            conf = float(np.mean([c for _, _, c in combo]))
            votes = float(np.mean([v for _, v, _ in combo]))      # nhiều biến thể cùng đọc ra = đáng tin hơn
            score = (is_valid(normalize(text)), votes)
            if best is None or score > best[0]:
                best = (score, text, conf)
        return best[1], best[2]


class AnprPipeline:
    def __init__(self):
        self.detector = self.ocr = None
        try:                                                # ưu tiên bộ 2 model YOLO (dò biển + dò ký tự)
            from anpr_yolo import YoloCharOCR, YoloPlateDetector, yolo_available
            if yolo_available():
                self.detector, self.ocr = YoloPlateDetector(), YoloCharOCR()
        except Exception as e:                              # thiếu thư viện / file hỏng -> quay về bộ cũ
            print("Không nạp được bộ YOLO LP_detector/LP_ocr, dùng bộ cũ:", e)
            self.detector = self.ocr = None
        if self.detector is None:
            self.detector, self.ocr = PlateDetector(), PlateOCR()

    def recognize(self, frame) -> PlateResult:
        with _INFER_LOCK:
            return self._recognize(frame)

    def _recognize(self, frame) -> PlateResult:
        det = self.detector.detect(frame)
        if det is None:
            return PlateResult(message="Không phát hiện được biển số trong ảnh")
        crop, box, dconf = det
        try:
            raw, oconf = self.ocr.read(crop)
        except RuntimeError as e:
            return PlateResult(crop=crop, box=box, det_conf=dconf, message=str(e))
        key = normalize(raw)
        valid = is_valid(key) and oconf >= config.OCR_CONF and dconf >= config.DETECT_CONF
        msg = "" if valid else "OCR không chắc chắn hoặc sai định dạng biển số (lóa sáng / bẩn / mờ?)"
        return PlateResult(pretty(key) if key else "", key, dconf, oconf, crop, box, valid, msg)


def save_image(frame, kind: str, key: str) -> str:
    """Lưu ảnh vào storage/images/<ngày>/; trả về đường dẫn tương đối để ghi vào DB."""
    now = datetime.now()
    folder = config.IMAGE_DIR / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now.strftime('%H%M%S_%f')}_{kind}_{key}.jpg"
    cv2.imwrite(str(path), frame)
    return str(path.relative_to(config.BASE_DIR))
