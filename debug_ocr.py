"""Chẩn đoán nhận diện: python debug_ocr.py duong_dan_anh.png
Lưu ảnh vào thư mục debug/ và in kết quả từng bước để biết lỗi nằm ở dò biển hay ở OCR."""
import sys
from pathlib import Path

import cv2
import numpy as np

from anpr import AnprPipeline
from plate_utils import is_valid, normalize, pretty

if len(sys.argv) < 2:
    sys.exit("Cách dùng: python debug_ocr.py <ảnh>")
img = cv2.imdecode(np.fromfile(sys.argv[1], dtype=np.uint8), cv2.IMREAD_COLOR)
if img is None:
    sys.exit("Không đọc được ảnh")
out = Path("debug")
out.mkdir(exist_ok=True)

_pl = AnprPipeline()                                   # đúng bộ dò/đọc mà app đang dùng
det = _pl.detector
print(f"[1] Bộ dò biển: {det.mode.upper()}" + ("  (chưa có models/LP_detector.pt + LP_ocr.pt -> đang dùng bộ dự phòng)" if det.mode != "yolo" else ""))
r = det.detect(img)
if r is None:
    sys.exit("[1] KHÔNG phát hiện được biển số -> lỗi nằm ở bước DÒ BIỂN (xem README: dùng model YOLO)")
crop, box, conf = r
vis = img.copy()
cv2.rectangle(vis, box[:2], box[2:], (0, 255, 0), 3)
cv2.imwrite(str(out / "1_detect.png"), vis)
cv2.imwrite(str(out / "2_crop.png"), crop)
print(f"[1] Box={box} conf={conf:.2f} kích thước crop={crop.shape[1]}x{crop.shape[0]}  -> xem debug/1_detect.png, debug/2_crop.png")
print("    (nếu khung xanh không ôm đúng biển số thì lỗi ở bước dò biển)")

ocr = _pl.ocr
if ocr.engine is None:
    sys.exit(ocr.error)
print(f"[2] OCR backend: {ocr.backend}")
text, conf = ocr.read(crop)
for li, name, raw, c, im in ocr.last_debug:
    cv2.imwrite(str(out / f"3_line{li}_{name}.png"), im)
    print(f"    dòng {li} | {name:6s} | đọc: {raw!r:18} | conf {c:.2f}")
key = normalize(text)
print(f"[3] Kết quả: thô={text!r} -> chuẩn hóa={key!r} -> {pretty(key)}  | đúng định dạng: {is_valid(key)} | conf={conf:.2f}")
