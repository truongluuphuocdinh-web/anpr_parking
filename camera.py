"""Nguồn camera: camera USB/webcam trên máy tính HOẶC camera điện thoại qua mạng (IP Webcam, DroidCam, RTSP...).

CameraStream đọc khung hình ở luồng nền và chỉ giữ khung MỚI NHẤT. Nhờ vậy:
  - luồng mạng (điện thoại) không bị dồn bộ đệm -> hình không bị trễ nhiều giây;
  - mở/đọc camera chậm hay rớt mạng cũng không làm đơ giao diện;
  - luồng mạng bị đứt thì tự kết nối lại.
"""
import json
import sys
import threading
import time
from urllib.parse import urlparse

import cv2

import config


def normalize_url(text: str) -> str:
    """'192.168.1.5:8080' -> 'http://192.168.1.5:8080/video' (IP Webcam / DroidCam). rtsp:// giữ nguyên."""
    t = (text or "").strip()
    if not t:
        return ""
    if "://" not in t:
        t = "http://" + t
    u = urlparse(t)
    if u.scheme in ("http", "https") and u.path in ("", "/"):
        t = t.rstrip("/") + "/video"
    return t


def load_phone_url() -> str:
    try:
        return json.loads(config.CAMERA_FILE.read_text(encoding="utf-8")).get("phone_url", "")
    except Exception:
        return ""


def save_phone_url(url: str):
    config.CAMERA_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CAMERA_FILE.write_text(json.dumps({"phone_url": url}), encoding="utf-8")


class CameraStream:
    def __init__(self, max_width=1280):
        self.max_width = max_width                          # khung quá lớn thì thu nhỏ để nhận diện nhanh hơn
        self.source, self.cap = None, None
        self.lock = threading.Lock()
        self._frame, self._seq, self._read_seq, self._last_ok = None, 0, 0, 0.0
        self._gen = 0                                       # đổi nguồn -> luồng cũ tự dừng
        self.error = ""

    # ---- mở / đóng ----
    def _open_cap(self, source):
        if isinstance(source, int):
            api = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY      # DSHOW mở webcam nhanh hơn trên Windows
            cap = cv2.VideoCapture(source, api)
        else:
            cap = cv2.VideoCapture(source)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap

    def open(self, source) -> bool:
        """Mở nguồn (số = camera USB, chuỗi = URL/đường dẫn). Có thể chậm vài giây -> gọi từ luồng nền."""
        self.release()
        cap = self._open_cap(source)
        ok = bool(cap.isOpened())
        if ok:                                               # thử đọc 1 khung để chắc chắn có hình
            for _ in range(10):
                ok, f = cap.read()
                if ok:
                    self._store(f)
                    break
                time.sleep(0.1)
        if not ok:
            cap.release()
            self.error = f"Không mở được nguồn camera: {source}"
            return False
        self.error, self.source, self.cap = "", source, cap
        self._gen += 1
        threading.Thread(target=self._loop, args=(self._gen,), daemon=True).start()
        return True

    def release(self):
        self._gen += 1                                       # báo luồng đọc dừng; luồng đó tự giải phóng camera khi thoát
        self.cap, self.source = None, None
        with self.lock:
            self._frame, self._seq, self._read_seq = None, 0, 0

    def isOpened(self) -> bool:
        return self.cap is not None and self.cap.isOpened()

    # ---- đọc ----
    def _store(self, f):
        if self.max_width and f.shape[1] > self.max_width:
            s = self.max_width / f.shape[1]
            f = cv2.resize(f, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        with self.lock:
            self._frame, self._seq, self._last_ok = f, self._seq + 1, time.time()

    def _loop(self, gen):
        cap, fails = self.cap, 0
        while gen == self._gen:
            ok, f = cap.read()
            if ok:
                fails = 0
                self._store(f)
                continue
            fails += 1
            time.sleep(0.05)
            if fails >= 40 and isinstance(self.source, str):          # luồng mạng đứt -> thử nối lại
                cap.release()
                time.sleep(1.5)
                if gen != self._gen:
                    return
                cap = self._open_cap(self.source)
                self.cap, fails = cap, 0
            elif fails >= 40:
                time.sleep(0.5)
        cap.release()

    def read(self):
        """(True, khung mới) nếu có khung chưa đọc; (False, None) nếu chưa có khung mới."""
        with self.lock:
            if self._frame is None or self._seq == self._read_seq:
                return False, None
            self._read_seq = self._seq
            return True, self._frame

    def stale(self, seconds=3.0) -> bool:
        """True nếu đã lâu không nhận được hình (mất kết nối)."""
        return self.isOpened() and time.time() - self._last_ok > seconds
