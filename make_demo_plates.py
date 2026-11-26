"""Tạo ảnh biển số mẫu để hiển thị trên laptop 'khách hàng' khi demo.
Chạy:  python make_demo_plates.py 59D1-123.45 51F-123.45 59O1-123.45
Ảnh lưu ở demo_plates/. Mở ảnh toàn màn hình trên laptop khách hàng, độ sáng vừa phải."""
import sys
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent / "demo_plates"


def make(plate: str, path: Path):
    img = np.full((480, 640, 3), 90, np.uint8)
    cv2.rectangle(img, (0, 330), (640, 480), (60, 60, 60), -1)          # nền đường
    font, th = cv2.FONT_HERSHEY_DUPLEX, 6
    if "-" in plate and len(plate.split("-")[0]) >= 4:                    # xe máy: 2 dòng
        top, bottom = plate.split("-")
        x1, y1, x2, y2 = 200, 150, 440, 330
        cv2.rectangle(img, (x1, y1), (x2, y2), (245, 245, 245), -1)
        cv2.rectangle(img, (x1, y1), (x2, y2), (20, 20, 20), 5)
        for txt, y in ((top, 225), (bottom, 305)):
            (tw, _), _ = cv2.getTextSize(txt, font, 1.8, th)
            cv2.putText(img, txt, (x1 + (x2 - x1 - tw) // 2, y), font, 1.8, (10, 10, 10), th)
    else:                                                                  # ô tô: 1 dòng
        x1, y1, x2, y2 = 120, 190, 520, 290
        cv2.rectangle(img, (x1, y1), (x2, y2), (245, 245, 245), -1)
        cv2.rectangle(img, (x1, y1), (x2, y2), (20, 20, 20), 5)
        (tw, _), _ = cv2.getTextSize(plate, font, 1.8, th)
        cv2.putText(img, plate, (x1 + (x2 - x1 - tw) // 2, 262), font, 1.8, (10, 10, 10), th)
    cv2.imwrite(str(path), img)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for p in (sys.argv[1:] or ["59D1-123.45", "51F-123.45", "59O1-123.45"]):
        make(p, OUT / f"{p.replace('.', '').replace('-', '_')}.png")
        print("Đã tạo", p)
