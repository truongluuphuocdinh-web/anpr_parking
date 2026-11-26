"""Kiểm tra 2 file model trong thư mục models/ đã nạp được chưa.

Chạy:   python check_models.py              -> kiểm tra file + nạp thử
        python check_models.py anh.jpg      -> kiểm tra + nhận diện thử 1 ảnh, lưu ảnh có khung vào debug/check.png
"""
import re
import sys
from pathlib import Path

import config


def describe(path, role):
    """In thông tin 1 file model. Trả về YoloModel hoặc None."""
    print(f"\n[{role}] {path.name}")
    if not path.exists():
        print(f"  X CHƯA CÓ file. Hãy chép vào: {path}")
        return None
    print(f"  - Dung lượng: {path.stat().st_size / 1e6:.1f} MB")
    try:
        from anpr_yolo import YoloModel
        m = YoloModel(path)
    except Exception as e:
        print(f"  X Nạp lỗi: {e}")
        print("  -> File là YOLOv5 thì chạy:  pip install yolov5   | là YOLOv8 thì:  pip install ultralytics")
        return None
    names = [str(v) for _, v in sorted(dict(m.model.names).items())] if not isinstance(m.model.names, list) \
        else [str(v) for v in m.model.names]
    print(f"  OK Nạp được (kiểu {m.kind}), {len(names)} class: {' '.join(names[:40])}{' ...' if len(names) > 40 else ''}")
    return m, names


def check_char_names(names):
    bad = [n for n in names if not re.fullmatch(r"[0-9A-Za-z]", n)]
    if bad:
        print(f"  ! Cảnh báo: class không phải 1 ký tự: {bad[:10]}. Model này có đúng là model ĐỌC KÝ TỰ không?")
        print("    (có thể bạn đang để nhầm file: model dò biển <-> model đọc ký tự)")
    elif len(names) < 30:
        print(f"  ! Cảnh báo: chỉ có {len(names)} class, biển VN cần ~31 (0-9, A-Z trừ vài chữ). Kiểm tra lại.")
    else:
        print("  OK Danh sách class đúng kiểu ký tự.")


def main():
    det = describe(Path(config.LP_DETECTOR_PATH), "Model DÒ BIỂN  (LP_detector.pt)")
    ocr = describe(Path(config.LP_OCR_PATH), "Model ĐỌC KÝ TỰ (LP_ocr.pt)")
    if det and len(det[1]) > 5:
        print("  ! Cảnh báo: model dò biển thường chỉ có 1-2 class, file này có nhiều class -> có thể bị đặt nhầm chỗ.")
    if ocr:
        check_char_names(ocr[1])
    if not (det and ocr):
        print("\n=> Chưa đủ 2 model, app sẽ dùng bộ dự phòng (RapidOCR + dò contour).")
        return
    print("\n=> Cả 2 model nạp được. App sẽ tự dùng chúng.")
    if len(sys.argv) > 1:
        import cv2
        import numpy as np
        from anpr import AnprPipeline
        from live import LiveState, draw_overlay
        img = cv2.imdecode(np.fromfile(sys.argv[1], dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            print("Không đọc được ảnh", sys.argv[1])
            return
        r = AnprPipeline().recognize(img)
        print(f"\nKết quả: '{r.text}'  hợp lệ={r.valid}  dò={r.det_conf:.2f}  đọc={r.ocr_conf:.2f}  {r.message}")
        if r.box:
            out = draw_overlay(img, LiveState(r.box, r.text, r.ocr_conf, r.valid, r.key, 0))
            Path("debug").mkdir(exist_ok=True)
            cv2.imwrite("debug/check.png", out)
            print("Đã lưu ảnh có khung: debug/check.png")


if __name__ == "__main__":
    main()
