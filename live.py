"""Nhận diện trực tiếp trên video: chạy nền, bỏ phiếu nhiều khung hình, vẽ khung + chữ lên hình.

Luồng giao diện chỉ làm 2 việc rất nhẹ:
    live.submit(frame)             # đưa khung hình mới nhất cho luồng nền
    draw_overlay(frame, live.snapshot())   # vẽ khung + chữ lên bản sao để hiển thị
Luồng nền tự xử lý với tốc độ máy chịu được (bỏ qua khung cũ), nên video không bị đơ.
"""
import threading
import time
from collections import deque

import cv2

from plate_utils import is_valid, pretty

GREEN, ORANGE = (60, 190, 60), (0, 150, 255)        # BGR


class LiveState:
    """Kết quả hiện tại để vẽ."""

    def __init__(self, box=None, text="", conf=0.0, stable=False, key="", ts=0.0, ms=0.0):
        self.box, self.text, self.conf, self.stable, self.key, self.ts = box, text, conf, stable, key, ts
        self.ms = ms                                         # thời gian dò biển + đọc ký tự của khung này (mili-giây)


class LiveRecognizer:
    def __init__(self, get_pipeline, history=6, window=2.0, show_unsure=True):
        self.get_pipeline = get_pipeline                    # hàm trả về AnprPipeline (hoặc None khi chưa tải xong)
        self.window, self.show_unsure = window, show_unsure
        self.min_votes = 2                                  # cần ít nhất N khung hình đọc giống nhau mới coi là chắc (ảnh tĩnh: 1)
        self.hist = deque(maxlen=history)                   # (thời điểm, key, conf, valid)
        self.lock = threading.Lock()
        self.state = LiveState()
        self.version = 0                                    # tăng mỗi khi kết quả đổi -> giao diện biết cần vẽ lại
        self._frame, self._fid, self._done = None, 0, 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.enabled = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ---- giao diện gọi ----
    def submit(self, frame):
        with self.lock:
            self._frame, self._fid = frame, self._fid + 1
        self._wake.set()

    def reset(self):
        with self.lock:
            self.hist.clear()
            self.state = LiveState()
            self.version += 1

    def snapshot(self, max_age=None):
        with self.lock:
            s = self.state
        if s.box is None or (max_age is not None and time.time() - s.ts > max_age):
            return None
        return s

    def stable_key(self, max_age=2.0):
        """Biển đã được >=min_votes khung hình đọc giống nhau & đúng định dạng (hoặc None)."""
        s = self.snapshot(max_age)
        return s.key if s and s.stable else None

    def stop(self):
        self._stop.set()
        self._wake.set()

    # ---- bỏ phiếu ----
    def _vote(self, now):
        """Trả về (key, conf, stable) theo đa số trong cửa sổ thời gian."""
        recent = [h for h in self.hist if now - h[0] <= self.window]
        score, conf, cnt = {}, {}, {}
        for _, key, c, valid in recent:
            if valid:
                score[key] = score.get(key, 0.0) + c
                conf[key] = max(conf.get(key, 0.0), c)
                cnt[key] = cnt.get(key, 0) + 1
        if score:
            best = max(score, key=score.get)
            return best, conf[best], cnt[best] >= self.min_votes
        return None, 0.0, False

    # ---- luồng nền ----
    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            if self._stop.is_set():
                break
            with self.lock:
                frame, fid = self._frame, self._fid
            pipeline = self.get_pipeline()
            if frame is None or fid == self._done or pipeline is None or not self.enabled:
                continue
            self._done = fid
            t0 = time.perf_counter()
            try:
                res = pipeline.recognize(frame)
            except Exception:
                continue
            self._update(res, (time.perf_counter() - t0) * 1000)
            if self._fid != self._done:                    # trong lúc xử lý đã có khung mới -> làm tiếp ngay
                self._wake.set()

    def _update(self, res, ms=0.0):
        now = time.time()
        with self.lock:
            if res.box is None:                            # không thấy biển
                self.hist.append((now, "", 0.0, False))
                if all(not h[1] for h in self.hist) or now - self.state.ts > 1.0:
                    self.state = LiveState()
                self.version += 1
                return
            key = res.key or ""
            valid = bool(res.valid and is_valid(key))
            self.hist.append((now, key, float(res.ocr_conf), valid))
            vkey, vconf, stable = self._vote(now)
            if vkey:                                       # có biển hợp lệ -> hiện theo đa số phiếu
                self.state = LiveState(res.box, pretty(vkey), vconf, stable, vkey, now, ms)
            elif self.show_unsure and len(key) >= 5:       # chưa chắc -> vẫn hiện chữ đọc được, màu cam
                self.state = LiveState(res.box, res.text or key, float(res.ocr_conf), False, "", now, ms)
            else:
                self.state = LiveState(res.box, "", 0.0, False, "", now, ms)
            self.version += 1


def draw_overlay(frame, state, badge=None):
    """Vẽ khung quanh biển + nhãn chữ nhỏ lên bản sao của frame. Trả về ảnh mới.
    badge = (chữ không dấu, màu BGR) vẽ thành nhãn dưới khung, dùng để báo vé tháng / hết hạn."""
    out = frame.copy()
    if state is None or state.box is None:
        return out
    x1, y1, x2, y2 = [int(v) for v in state.box]
    color = GREEN if state.stable else ORANGE
    h, w = out.shape[:2]
    th = max(2, round(w / 400))
    cv2.rectangle(out, (x1, y1), (x2, y2), color, th)
    d = max(12, (x2 - x1) // 8)                            # 4 góc đậm cho dễ nhìn
    for (cx, cy, sx, sy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
        cv2.line(out, (cx, cy), (cx + sx * d, cy), color, th + 2)
        cv2.line(out, (cx, cy), (cx, cy + sy * d), color, th + 2)
    if state.text:
        label = f"{state.text}  {int(state.conf * 100)}%" if state.conf else state.text
        if not state.stable:
            label += " ?"
        scale = max(0.45, w / 1400)
        (tw, tt), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        pad = 4
        ly = y1 - 6 if y1 - tt - 2 * pad > 0 else y2 + tt + 2 * pad      # hết chỗ phía trên thì đặt xuống dưới
        top = ly - tt - pad
        cv2.rectangle(out, (x1, top), (x1 + tw + 2 * pad, ly + base), color, -1)
        cv2.putText(out, label, (x1 + pad, ly), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)
    if badge and badge[0]:
        text, bcolor = badge
        scale = max(0.5, w / 1300)
        (tw, tt), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        pad = 5
        top = y2 + 6 if y2 + 6 + tt + 2 * pad < h else max(0, y1 - 2 * tt - 4 * pad)   # hết chỗ dưới thì đặt phía trên
        cv2.rectangle(out, (x1, top), (x1 + tw + 2 * pad, top + tt + 2 * pad + base), bcolor, -1)
        cv2.putText(out, text, (x1 + pad, top + tt + pad), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2,
                    cv2.LINE_AA)
    return out
