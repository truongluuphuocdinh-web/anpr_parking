"""Cấu hình toàn hệ thống."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "parking.db"
IMAGE_DIR = BASE_DIR / "storage" / "images"
EXPORT_DIR = BASE_DIR / "storage" / "exports"
YOLO_MODEL_PATH = BASE_DIR / "models" / "plate.pt"   # (cũ) YOLO dò biển số, đọc chữ vẫn bằng RapidOCR
# Bộ 2 model YOLO kiểu repo GitHub biển số VN: nếu có CẢ HAI file thì tự động dùng, thay cho dò contour + RapidOCR
LP_DETECTOR_PATH = BASE_DIR / "models" / "LP_detector.pt"   # dò khung biển số
LP_OCR_PATH = BASE_DIR / "models" / "LP_ocr.pt"             # dò từng ký tự trong biển

CAMERA_INDEX = 0          # camera USB/webcam trên máy tính: 0 = mặc định, 1 = camera rời thứ hai...
CAMERA_FILE = BASE_DIR / "storage" / "camera.json"   # nhớ địa chỉ camera điện thoại đã nhập
DETECT_CONF = 0.5         # ngưỡng tin cậy YOLO
OCR_BACKEND = "auto"      # "auto" (RapidOCR trước, EasyOCR sau) | "rapidocr" | "easyocr"
OCR_CONF = 0.4            # ngưỡng tin cậy OCR
AUTO_ACCEPT_CONF = 0.90   # máy đọc đúng định dạng nhưng tin cậy < ngưỡng này -> bảo vệ phải xác nhận/sửa trước khi cho vào/ra
                          # (đặt 1.1 = LUÔN xác nhận, đặt 0 = không bao giờ hỏi)
SUB_WARN_DAYS = 7          # vé tháng còn <= N ngày thì cảnh báo "sắp hết hạn"
VOID_WINDOW_MIN = 10      # bảo vệ chỉ được hủy lượt vào nhập nhầm trong N phút đầu; Admin hủy được bất cứ lúc nào

UI_COMPACT = False        # True = ẩn khung nhật ký để vừa màn hình thấp (laptop 768px); cửa sổ chính cao ~820px
PERF_TARGET_S = 1.5       # NFR đề 7: nhận diện + mở barrier <= 1.5 giây
ACCURACY_TARGET = 0.95    # NFR đề 7: độ chính xác mức ký tự > 95% (ban ngày)

# REST API + Swagger UI (api_server.py) cho máy thứ hai trong CÙNG mạng LAN: http://<IP>:8000/docs
# Đặt API_ENABLED = False để tắt.
API_ENABLED = True
API_HOST = "0.0.0.0"         # 0.0.0.0 = cho máy khác trong mạng vào; "127.0.0.1" = chỉ máy này
API_PORT = 8000

# Web Admin HTML cũ (admin_web.py) - đã được thay bằng REST API ở trên, mặc định TẮT.
# Nếu bật thì nên đổi cổng khác API_PORT.
ADMIN_WEB_ENABLED = False
ADMIN_WEB_HOST = "0.0.0.0"
ADMIN_WEB_PORT = 8001

SLOT_ROWS = "ABC"         # hàng A, B, C
SLOT_COLS = 3             # cột 1, 2, 3

NEAR_DISTANCE = 1         # Levenshtein <= 1 -> cảnh báo vàng
TOP_N = 5                 # số xe gợi ý khi mất thẻ

# Số liệu giả định, đổi theo đề bài
DEFAULT_PRICING = dict(name="Mặc định", free_minutes=15, block_minutes=60,
                       block_price=5000, daily_cap=30000, lost_ticket_fee=50000)
# (username, password, họ tên, vai trò) - nhớ đổi mật khẩu khi dùng thật
DEFAULT_USERS = [("admin", "admin123", "Quản trị viên", "ADMIN"),
                 ("baove", "baove123", "Bảo vệ", "GUARD")]
