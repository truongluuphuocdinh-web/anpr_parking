"""Chấm điểm bộ nhận diện trên một thư mục ảnh biển số để làm MINH CHỨNG cho 2 chỉ tiêu phi chức năng của đề 7:
   - Độ chính xác mức ký tự  > 95% (ban ngày)
   - Tổng thời gian nhận diện <= 1.5 giây

Cách dùng:
    python evaluate.py thu_muc_anh
Nhãn đúng (biển thật) lấy từ TÊN FILE, phần trước dấu "__" (hoặc cả tên nếu không có):
    59D1-123.45.jpg      59D1-123.45__ban_ngay_2.jpg      30A12345__toi.png
Hoặc đặt file labels.csv trong thư mục với 2 cột:  file,plate
Kết quả in ra màn hình và lưu storage/exports/accuracy_report.csv
"""
import csv
import sys
import time
from pathlib import Path

import config
from plate_utils import is_valid, levenshtein, normalize

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def load_labels(folder: Path):
    """Trả về danh sách (đường dẫn ảnh, biển đúng đã chuẩn hóa)."""
    folder, pairs, table = Path(folder), [], {}
    csv_path = folder / "labels.csv"
    if csv_path.exists():
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                table[row["file"].strip()] = row["plate"]
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() not in IMG_EXT:
            continue
        raw = table.get(p.name) or p.stem.split("__")[0]
        key = normalize(raw)
        if is_valid(key):
            pairs.append((p, key))
        else:
            print(f"  ! Bỏ qua {p.name}: không suy ra được biển đúng từ '{raw}'")
    return pairs


def score(pred: str, truth: str):
    d = levenshtein(pred, truth)
    return d, max(0.0, 1 - d / max(len(truth), 1))


def evaluate(items, recognize):
    """items: [(đường dẫn, biển đúng)]; recognize(đường dẫn) -> biển máy đọc (key). Trả về dict kết quả."""
    rows, tot_err, tot_len, exact, times = [], 0, 0, 0, []
    for path, truth in items:
        t0 = time.perf_counter()
        try:
            pred = recognize(path) or ""
        except Exception as e:
            print(f"  ! Lỗi khi đọc {Path(path).name}: {e}")
            pred = ""
        ms = (time.perf_counter() - t0) * 1000
        d, acc = score(pred, truth)
        tot_err, tot_len, exact = tot_err + d, tot_len + len(truth), exact + (pred == truth)
        times.append(ms)
        rows.append(dict(file=Path(path).name, truth=truth, pred=pred, errors=d, char_acc=acc, ms=ms))
    n = len(rows)
    ts = sorted(times)
    return dict(
        n=n, plate_acc=exact / n if n else 0.0,
        char_acc=1 - tot_err / tot_len if tot_len else 0.0,
        avg_ms=sum(times) / n if n else 0.0, p95_ms=ts[min(n - 1, int(round(0.95 * (n - 1))))] if n else 0.0,
        rows=rows)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    import cv2
    import numpy as np
    from anpr import AnprPipeline
    items = load_labels(Path(sys.argv[1]))
    if not items:
        print("Không có ảnh hợp lệ để chấm.")
        return
    pipe = AnprPipeline()
    print(f"Bộ dò: {getattr(pipe.detector, 'mode', '?')} | Bộ đọc: {type(pipe.ocr).__name__} | {len(items)} ảnh\n")
    pipe.recognize(np.zeros((240, 320, 3), np.uint8))                  # chạy khởi động 1 lần cho công bằng

    def rec(path):
        img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        return pipe.recognize(img).key

    r = evaluate(items, rec)
    for x in r["rows"]:
        mark = "OK " if x["errors"] == 0 else "SAI"
        print(f"  [{mark}] {x['file'][:34]:34} đúng={x['truth']:11} máy đọc={x['pred']:11} {x['ms']:6.0f} ms")
    ca, tg = r["char_acc"], config.ACCURACY_TARGET
    print(f"\nSố ảnh: {r['n']}")
    print(f"Đúng nguyên biển        : {r['plate_acc'] * 100:5.1f}%")
    print(f"Độ chính xác mức ký tự  : {ca * 100:5.1f}%   (chỉ tiêu > {tg * 100:.0f}%)  -> {'ĐẠT' if ca > tg else 'CHƯA ĐẠT'}")
    ok_t = r["p95_ms"] / 1000 <= config.PERF_TARGET_S
    print(f"Thời gian nhận diện     : trung bình {r['avg_ms']:.0f} ms, P95 {r['p95_ms']:.0f} ms "
          f"(chỉ tiêu <= {config.PERF_TARGET_S * 1000:.0f} ms) -> {'ĐẠT' if ok_t else 'CHƯA ĐẠT'}")
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = config.EXPORT_DIR / "accuracy_report.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(r["rows"][0].keys()))
        w.writeheader()
        w.writerows(r["rows"])
    print(f"\nĐã lưu báo cáo chi tiết: {out}")
    print("Lưu ý: kết quả chỉ có giá trị khi ảnh thử là ảnh THẬT, chụp ngoài đời ban ngày, và không dùng lại ảnh đã train.")


if __name__ == "__main__":
    main()
