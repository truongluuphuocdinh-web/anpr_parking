"""REST API + Swagger UI cho hệ thống quản lý bãi giữ xe ANPR.

Thay cho web Admin HTML cũ (admin_web.py): mọi chức năng được mở ra dưới dạng API JSON và được tài liệu hóa
bằng Swagger UI (chuẩn OpenAPI 3).

  Swagger UI ........ http://<IP máy bảo vệ>:8000/docs
  File mô tả API .... http://<IP máy bảo vệ>:8000/openapi.json   (hoặc chạy `python export_openapi.py` -> openapi.json)

Cách thử trên Swagger UI: bấm **Authorize** -> nhập tài khoản/mật khẩu (admin / admin123) -> gọi API bằng **Try it out**.
Nghiệp vụ dùng lại 100% `services.ParkingService` nên Audit Log, tính phí, vé tháng... giống hệt app Tkinter.

Bảo mật (chỉ dùng trong mạng nội bộ, KHÔNG đưa ra internet - chưa có HTTPS):
  - Bearer token ngẫu nhiên, hết hạn sau 30 phút không dùng; phân quyền GUARD / ADMIN
  - Sai mật khẩu quá 5 lần / 5 phút từ một địa chỉ -> tạm khóa; đăng nhập sai/đổi mật khẩu đều vào Audit Log
"""
import secrets
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import List, Literal, Optional

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from db import Database
from plate_utils import is_valid, normalize, pretty
from services import AUDIT_SENSITIVE, EVENT_LABELS, ParkingService

SESSION_TTL = 30 * 60
MAX_FAILS, FAIL_WINDOW = 5, 300
MAX_UPLOAD = 8 * 1024 * 1024
STATIC_DIR = config.BASE_DIR / "static" / "swagger"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

DESCRIPTION = """
API của **Hệ thống quản lý bãi giữ xe ANPR** (đồ án DAMH): nhận diện biển số, check-in / check-out, tính phí,
vé tháng, mất thẻ, thống kê và Audit Log. Bãi giả lập 3x3 (ô A1..C3).

### Cách dùng nhanh
1. Bấm nút **Authorize** (góc phải) -> nhập `admin` / `admin123` (Admin) hoặc `baove` / `baove123` (Bảo vệ) -> **Authorize**.
2. Chọn một API -> **Try it out** -> **Execute**.
3. Tải toàn bộ mô tả API dạng JSON tại [`/openapi.json`](/openapi.json).

### Phân quyền
| Vai trò | Được dùng |
|---|---|
| `GUARD` (Bảo vệ) | Auth, Bãi xe, Cổng (quét biển, mất thẻ, di dời), Lịch sử |
| `ADMIN` | Tất cả của Bảo vệ + Vé tháng, Thống kê, Audit Log, Xuất Excel |

Mọi lỗi trả về JSON dạng `{"detail": "..."}` với mã HTTP tương ứng (400, 401, 403, 404, 409, 422, 429).
"""

TAGS = [
    {"name": "Hệ thống", "description": "Kiểm tra máy chủ còn sống."},
    {"name": "Xác thực", "description": "Đăng nhập lấy Bearer token, xem tài khoản, đổi mật khẩu."},
    {"name": "Bãi xe", "description": "Sơ đồ bãi 3x3 theo thời gian thực, di dời xe giữa các ô."},
    {"name": "Cổng", "description": "Nhận diện biển từ ảnh, quét biển (check-in / check-out), cảnh báo vàng, mất thẻ."},
    {"name": "Lượt gửi xe", "description": "Sửa biển / hủy lượt vào nhập nhầm, lịch sử ra/vào."},
    {"name": "Vé tháng", "description": "Chỉ ADMIN: thêm, gia hạn, xóa vé tháng; trạng thái còn hạn / sắp hết / hết hạn."},
    {"name": "Báo cáo", "description": "Chỉ ADMIN: thống kê theo ngày, hiệu năng cổng, Audit Log, xuất Excel."},
]


# ======================================================================
# Mô hình dữ liệu (hiển thị ở mục Schemas của Swagger UI)
# ======================================================================
class ErrorOut(BaseModel):
    detail: str = Field(..., examples=["Mô tả lỗi"])


class MessageOut(BaseModel):
    message: str = Field(..., examples=["Đã thực hiện"])


class HealthOut(BaseModel):
    status: str = Field(..., examples=["ok"])
    time: str = Field(..., examples=["2026-11-26 08:00:00"])
    slots_total: int = Field(..., examples=[9])


class LoginIn(BaseModel):
    username: str = Field(..., examples=["admin"])
    password: str = Field(..., examples=["admin123"])


class TokenOut(BaseModel):
    access_token: str = Field(..., description="Gửi kèm header `Authorization: Bearer <token>`")
    token_type: str = Field("bearer", examples=["bearer"])
    expires_in: int = Field(..., description="Số giây không thao tác thì token hết hạn", examples=[1800])
    username: str = Field(..., examples=["admin"])
    role: Literal["ADMIN", "GUARD"]


class UserOut(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    role: Literal["ADMIN", "GUARD"]
    using_default_password: bool = Field(..., description="True nếu vẫn dùng mật khẩu mặc định -> nên đổi ngay")


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6, description="Tối thiểu 6 ký tự")


class SubStatusOut(BaseModel):
    status: Literal["ACTIVE", "EXPIRED", "NOT_STARTED", "NONE"] = Field(
        ..., description="ACTIVE = còn hạn, EXPIRED = hết hạn, NOT_STARTED = chưa hiệu lực, NONE = vé lẻ")
    soon: bool = Field(..., description="True nếu vé còn hạn nhưng sắp hết (<= SUB_WARN_DAYS ngày)")
    days: int = Field(..., description="ACTIVE: còn mấy ngày; EXPIRED: đã hết hạn mấy ngày")
    ref_date: str = Field("", description="ACTIVE/EXPIRED: ngày hết hạn; NOT_STARTED: ngày bắt đầu (YYYY-MM-DD)")
    owner: str = ""
    label: str = Field(..., examples=["VÉ THÁNG còn 23 ngày (đến 19/12/2026)"])


class SlotOut(BaseModel):
    code: str = Field(..., examples=["A1"])
    occupied: bool
    log_id: Optional[int] = None
    plate: Optional[str] = Field(None, examples=["59D1-123.45"])
    time_in: Optional[str] = Field(None, examples=["2026-11-26 07:45:10"])
    subscription: Optional[SubStatusOut] = None


class LotOut(BaseModel):
    total: int = Field(..., examples=[9])
    occupied: int = Field(..., examples=[2])
    free: int = Field(..., examples=[7])
    slots: List[SlotOut]


class ScanIn(BaseModel):
    plate: str = Field(..., description="Biển số (máy đọc được hoặc bảo vệ nhập tay). Chấp nhận có/không dấu `-` `.`",
                       examples=["59D1-123.45"])
    method: Literal["AUTO", "MANUAL"] = Field("AUTO", description="AUTO = máy đọc, MANUAL = bảo vệ nhập tay")


class ScanOut(BaseModel):
    action: Literal["CHECK_IN", "CHECK_OUT", "WARN_NEAR", "LOT_FULL"] = Field(
        ..., description="CHECK_IN = cho vào; CHECK_OUT = cho ra + tính phí; WARN_NEAR = biển lệch 1 ký tự "
                         "(cảnh báo vàng, cần xác nhận); LOT_FULL = bãi đầy")
    message: str
    plate: str = ""
    slot: str = ""
    fee: int = Field(0, description="Phí phải thu (đồng) khi CHECK_OUT")
    fee_type: str = Field("", description="SUBSCRIPTION | REGULAR | EXPIRED_SUB | LOST_TICKET")
    minutes: int = 0
    log_id: int = 0
    distance: int = Field(0, description="WARN_NEAR: khoảng cách Levenshtein tới biển đang trong bãi")
    near_log_id: Optional[int] = Field(None, description="WARN_NEAR: lượt đang trong bãi có biển gần giống")
    near_plate: Optional[str] = None
    subscription: Optional[SubStatusOut] = None


class RecognizeOut(BaseModel):
    text: str = Field("", description="Biển đã định dạng", examples=["59D1-123.45"])
    key: str = Field("", description="Biển chỉ gồm chữ hoa + số", examples=["59D112345"])
    valid: bool = Field(..., description="Đúng định dạng và độ tin cậy đạt ngưỡng")
    det_conf: float = Field(..., description="Độ tin cậy dò khung biển (0..1)")
    ocr_conf: float = Field(..., description="Độ tin cậy đọc ký tự (0..1)")
    box: Optional[List[int]] = Field(None, description="Khung biển [x1, y1, x2, y2] trên ảnh")
    message: str = ""
    ms: float = Field(..., description="Thời gian nhận diện (ms)")


class ConfirmNearIn(BaseModel):
    log_id: int = Field(..., description="`near_log_id` trả về từ API quét biển", examples=[12])
    read_text: str = Field("", description="Biển máy đọc được ở cổng ra", examples=["59O1-123.45"])


class LostCandidateOut(BaseModel):
    log_id: int
    plate: str
    slot: Optional[str] = None
    time_in: str
    distance: int = Field(..., description="Khoảng cách Levenshtein tới biển tìm kiếm (càng nhỏ càng giống)")


class LostCheckoutIn(BaseModel):
    log_id: int = Field(..., description="Lượt vào bảo vệ chọn là đúng xe", examples=[12])
    read_text: str = Field("", description="Biển đọc được tại cổng (nếu có)")


class MoveIn(BaseModel):
    from_slot: str = Field(..., examples=["A1"])
    to_slot: str = Field(..., examples=["B2"])


class PlateFixIn(BaseModel):
    plate: str = Field(..., description="Biển đúng", examples=["59D1-123.45"])


class LogOut(BaseModel):
    id: int
    plate_text: str
    time_in: str
    time_out: Optional[str] = None
    slot_code: Optional[str] = None
    status: Literal["IN", "OUT"]
    fee: int = 0
    fee_type: Optional[str] = None
    in_method: Optional[str] = None
    out_method: Optional[str] = None


class HistoryOut(BaseModel):
    items: List[LogOut]
    next_before: Optional[int] = Field(None, description="Truyền vào tham số `before` để lấy trang cũ hơn; null = hết")


class SubIn(BaseModel):
    plate: str = Field(..., examples=["59D1-123.45"])
    owner: str = Field("", max_length=60, examples=["Nguyễn Văn An"])
    start_date: date = Field(..., examples=["2026-11-26"])
    end_date: date = Field(..., examples=["2026-12-26"])


class SubOut(BaseModel):
    id: int
    plate: str
    owner: str
    start_date: str
    end_date: str
    status: Literal["ACTIVE", "SOON", "EXPIRED", "NOT_STARTED"] = Field(
        ..., description="SOON = còn hạn nhưng sắp hết (<= SUB_WARN_DAYS ngày)")
    days: int = Field(..., description="Số ngày còn lại (ACTIVE/SOON) hoặc đã quá hạn (EXPIRED)")


class DailyStatOut(BaseModel):
    day: str = Field(..., examples=["2026-11-26"])
    vao: int = Field(..., description="Số xe vào")
    ra: int = Field(..., description="Số xe ra")
    doanh_thu: int = Field(..., description="Doanh thu (đồng)")


class PerfOut(BaseModel):
    target_seconds: float = Field(..., examples=[1.5])
    n: int = Field(..., description="Số lượt quét đã đo")
    p50_ms: float
    p95_ms: float
    avg_ms: float
    max_ms: float
    ok_pct: float = Field(..., description="% lượt đạt mục tiêu (nhận diện + mở barrier <= 1.5 s)")


class AuditOut(BaseModel):
    id: int
    event_time: str
    event_type: str
    action: str = Field(..., description="Tên hành động tiếng Việt", examples=["Bảo vệ sửa biển máy đọc"])
    username: Optional[str] = None
    role: Optional[str] = None
    plate_text: Optional[str] = None
    detail: Optional[str] = None


class AuditPageOut(BaseModel):
    items: List[AuditOut]
    next_before: Optional[int] = None


# ======================================================================
# Ứng dụng
# ======================================================================
def _sub_out(info) -> dict:
    return dict(status=info.status, soon=info.soon, days=info.days, ref_date=info.ref_date, owner=info.owner,
                label=info.label)


def _scan_out(o) -> dict:
    return dict(action=o.action, message=o.message, plate=o.plate, slot=o.slot, fee=o.fee, fee_type=o.fee_type,
                minutes=o.minutes, log_id=o.log_id, distance=o.distance,
                near_log_id=o.near_log["id"] if o.near_log else None,
                near_plate=o.near_log["plate_text"] if o.near_log else None,
                subscription=_sub_out(o.sub) if o.sub else None)


def create_app(db_path=None, pipeline_getter=None) -> FastAPI:
    """Tạo ứng dụng FastAPI. `pipeline_getter()` trả về AnprPipeline đang dùng (None nếu chưa tải xong)."""
    db_path = str(db_path or config.DB_PATH)
    sessions, fails, lock = {}, {}, threading.Lock()
    err = {c: {"model": ErrorOut} for c in (400, 401, 403, 404, 409)}

    app = FastAPI(title="API Quản lý bãi giữ xe ANPR", version="1.0.0", description=DESCRIPTION,
                  openapi_tags=TAGS, docs_url=None, redoc_url=None,
                  contact={"name": "Đồ án DAMH - Hệ thống bãi giữ xe ANPR"})
    app.state.sessions = sessions
    oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False,
                                  description="Nhập tài khoản/mật khẩu hệ thống (admin / admin123).")

    # ---------- phụ thuộc ----------
    def get_svc():
        db = Database(db_path, check_same_thread=False)        # mỗi yêu cầu một kết nối riêng
        try:
            yield ParkingService(db)
        finally:
            db.conn.close()

    def current_user(token: Optional[str] = Depends(oauth2)) -> dict:
        with lock:
            s = sessions.get(token or "")
            if s and s["exp"] > time.time():
                s["exp"] = time.time() + SESSION_TTL
                return s
            sessions.pop(token or "", None)
        raise HTTPException(401, "Chưa đăng nhập hoặc phiên đã hết hạn", headers={"WWW-Authenticate": "Bearer"})

    def admin_only(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "ADMIN":
            raise HTTPException(403, "Chức năng này chỉ dành cho ADMIN")
        return user

    def do_login(svc: ParkingService, request: Request, username: str, password: str) -> dict:
        ip, now = request.client.host if request.client else "?", time.time()
        with lock:
            recent = [t for t in fails.get(ip, []) if now - t < FAIL_WINDOW]
            fails[ip] = recent
        if len(recent) >= MAX_FAILS:
            raise HTTPException(429, "Sai quá nhiều lần. Vui lòng thử lại sau 5 phút.")
        user = svc.login(username.strip(), password)
        if not user:
            with lock:
                fails.setdefault(ip, []).append(now)
            raise HTTPException(401, "Sai tài khoản hoặc mật khẩu", headers={"WWW-Authenticate": "Bearer"})
        token = secrets.token_urlsafe(32)
        with lock:
            sessions[token] = dict(user_id=user["id"], username=user["username"], role=user["role"],
                                   exp=now + SESSION_TTL, token=token)
            fails.pop(ip, None)
        svc.log_event("LOGIN", None, f"{user['username']} (API, {ip})", user["id"])
        return dict(access_token=token, token_type="bearer", expires_in=SESSION_TTL, username=user["username"],
                    role=user["role"])

    def fail(ex: Exception):
        """Lỗi nghiệp vụ của service -> mã HTTP phù hợp."""
        if isinstance(ex, PermissionError):
            raise HTTPException(403, str(ex))
        msg = str(ex)
        raise HTTPException(409 if ("đã có" in msg or "không còn" in msg or "đang trống" in msg) else 400, msg)

    def need_plate(raw: str) -> tuple:
        key = normalize(raw)
        if not is_valid(key):
            raise HTTPException(422, "Biển số không hợp lệ (ví dụ 59D1-123.45)")
        return key, pretty(key)

    def open_log(svc: ParkingService, log_id: int) -> dict:
        log = svc.db.get_log(log_id)
        if not log:
            raise HTTPException(404, "Không tìm thấy lượt gửi xe")
        if log["status"] != "IN":
            raise HTTPException(409, "Lượt này không còn trong bãi")
        return log

    # ---------- tài liệu: Swagger UI chạy offline (file chép sẵn trong static/swagger) ----------
    if STATIC_DIR.is_dir():
        app.mount("/static/swagger", StaticFiles(directory=str(STATIC_DIR)), name="swagger-static")
    local = (STATIC_DIR / "swagger-ui-bundle.js").exists() and (STATIC_DIR / "swagger-ui.css").exists()

    @app.get("/docs", include_in_schema=False)
    def swagger_ui():
        extra = dict(swagger_js_url="/static/swagger/swagger-ui-bundle.js",
                     swagger_css_url="/static/swagger/swagger-ui.css",
                     swagger_favicon_url="/static/swagger/favicon-32x32.png") if local else {}
        return get_swagger_ui_html(
            openapi_url="/openapi.json", title="Bãi xe ANPR - Swagger UI",
            swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True,
                                   "docExpansion": "list", "filter": True, "tryItOutEnabled": True},
            **extra)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    # ================= Hệ thống =================
    @app.get("/api/health", tags=["Hệ thống"], summary="Kiểm tra máy chủ", response_model=HealthOut)
    def health():
        """Không cần đăng nhập. Dùng để kiểm tra máy chủ API còn chạy."""
        from db import now_str
        return dict(status="ok", time=now_str(), slots_total=len(config.SLOT_ROWS) * config.SLOT_COLS)

    # ================= Xác thực =================
    @app.post("/api/auth/token", tags=["Xác thực"], summary="Đăng nhập (form - dùng cho nút Authorize)",
              response_model=TokenOut, responses={401: err[401], 429: {"model": ErrorOut}})
    def auth_token(request: Request, form: OAuth2PasswordRequestForm = Depends(), svc=Depends(get_svc)):
        """Đăng nhập theo chuẩn OAuth2 password flow (dạng form). Nút **Authorize** của Swagger UI gọi API này."""
        return do_login(svc, request, form.username, form.password)

    @app.post("/api/auth/login", tags=["Xác thực"], summary="Đăng nhập (JSON)", response_model=TokenOut,
              responses={401: err[401], 429: {"model": ErrorOut}})
    def auth_login(body: LoginIn, request: Request, svc=Depends(get_svc)):
        """Đăng nhập bằng JSON, trả về Bearer token. Gửi token ở header `Authorization: Bearer <token>`.
        Sai mật khẩu quá 5 lần / 5 phút từ một địa chỉ sẽ bị tạm khóa (429)."""
        return do_login(svc, request, body.username, body.password)

    @app.get("/api/auth/me", tags=["Xác thực"], summary="Thông tin tài khoản đang đăng nhập", response_model=UserOut,
             responses={401: err[401]})
    def auth_me(user=Depends(current_user), svc=Depends(get_svc)):
        u = svc.db.get_user(user["user_id"])
        default = any(du == u["username"] and svc.db.verify_user(du, dp) for du, dp, *_ in config.DEFAULT_USERS)
        return dict(**u, using_default_password=default)

    @app.post("/api/auth/logout", tags=["Xác thực"], summary="Đăng xuất (hủy token)", response_model=MessageOut,
              responses={401: err[401]})
    def auth_logout(user=Depends(current_user)):
        with lock:
            sessions.pop(user["token"], None)
        return dict(message="Đã đăng xuất")

    @app.post("/api/auth/password", tags=["Xác thực"], summary="Đổi mật khẩu", response_model=MessageOut,
              responses={400: err[400], 401: err[401]})
    def auth_password(body: PasswordIn, user=Depends(current_user), svc=Depends(get_svc)):
        """Cần nhập đúng mật khẩu hiện tại; mật khẩu mới tối thiểu 6 ký tự. Có ghi Audit Log."""
        try:
            svc.change_password(user["user_id"], body.old_password, body.new_password)
        except ValueError as ex:
            fail(ex)
        return dict(message="Đã đổi mật khẩu")

    # ================= Bãi xe =================
    @app.get("/api/lot", tags=["Bãi xe"], summary="Sơ đồ bãi realtime", response_model=LotOut,
             responses={401: err[401]})
    def lot(user=Depends(current_user), svc=Depends(get_svc)):
        """Trạng thái 9 ô đỗ (A1..C3): ô trống / ô có xe, biển số, giờ vào và loại vé (vé tháng / vé lẻ)."""
        occ = {l["slot_code"]: l for l in svc.db.open_logs()}
        slots = []
        for r in config.SLOT_ROWS:
            for c in range(1, config.SLOT_COLS + 1):
                code, log = f"{r}{c}", occ.get(f"{r}{c}")
                slots.append(dict(code=code, occupied=bool(log), log_id=log["id"] if log else None,
                                  plate=log["plate_text"] if log else None, time_in=log["time_in"] if log else None,
                                  subscription=_sub_out(svc.subscription_status(log["plate_key"])) if log else None))
        total = len(slots)
        return dict(total=total, occupied=len(occ), free=total - len(occ), slots=slots)

    @app.post("/api/lot/move", tags=["Bãi xe"], summary="Di dời xe sang ô khác", response_model=MessageOut,
              responses={400: err[400], 401: err[401], 409: err[409]})
    def lot_move(body: MoveIn, user=Depends(current_user), svc=Depends(get_svc)):
        """Chuyển xe từ ô `from_slot` sang ô trống `to_slot`. Ghi Audit Log (SLOT_MOVED)."""
        try:
            svc.move_car(body.from_slot.upper(), body.to_slot.upper(), user["user_id"])
        except ValueError as ex:
            fail(ex)
        return dict(message=f"Đã chuyển xe {body.from_slot.upper()} -> {body.to_slot.upper()}")

    # ================= Cổng =================
    @app.post("/api/gate/recognize", tags=["Cổng"], summary="Nhận diện biển số từ ảnh", response_model=RecognizeOut,
              responses={400: err[400], 401: err[401], 503: {"model": ErrorOut}})
    def gate_recognize(file: UploadFile = File(..., description="Ảnh JPG/PNG chứa biển số"),
                       user=Depends(current_user)):
        """Chạy pipeline YOLO + OCR trên ảnh tải lên, trả về biển đọc được và độ tin cậy.
        **Không** check-in/out: hãy gọi tiếp `POST /api/gate/scan` với biển này (hoặc biển bảo vệ đã sửa)."""
        pipeline = pipeline_getter() if pipeline_getter else None
        if pipeline is None:
            raise HTTPException(503, "Bộ nhận diện chưa sẵn sàng (đang tải mô hình). Hãy thử lại sau ít giây.")
        data = file.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(400, "Ảnh quá lớn (tối đa 8 MB)")
        import cv2
        import numpy as np
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(400, "Không đọc được file ảnh")
        t0 = time.perf_counter()
        res = pipeline.recognize(frame)
        ms = (time.perf_counter() - t0) * 1000
        return dict(text=res.text, key=res.key, valid=res.valid, det_conf=float(res.det_conf),
                    ocr_conf=float(res.ocr_conf), box=[int(v) for v in res.box] if res.box else None,
                    message=res.message, ms=round(ms, 1))

    @app.post("/api/gate/scan", tags=["Cổng"], summary="Quét biển: check-in / check-out", response_model=ScanOut,
              responses={401: err[401], 422: {"model": ErrorOut}})
    def gate_scan(body: ScanIn, user=Depends(current_user), svc=Depends(get_svc)):
        """Luồng chính của cổng bãi xe:
        - Biển **chưa** có trong bãi -> **CHECK_IN**, xe vào ô trống ngẫu nhiên (bãi đầy -> `LOT_FULL`).
        - Biển **khớp 100%** xe đang trong bãi -> **CHECK_OUT**, tính phí (vé tháng còn hạn = miễn phí).
        - Biển **lệch <= 1 ký tự** so với xe trong bãi -> **WARN_NEAR** (cảnh báo vàng): bảo vệ đối chiếu rồi gọi
          `POST /api/gate/confirm-near` (cùng xe) hoặc quét lại.
        - Biển sai định dạng -> 422, bảo vệ nhập lại."""
        key, text = need_plate(body.plate)
        t0 = time.perf_counter()
        out = svc.process_scan(text, key, None, body.method, user["user_id"])
        svc.record_perf("GATE", (time.perf_counter() - t0) * 1000)
        return _scan_out(out)

    @app.post("/api/gate/confirm-near", tags=["Cổng"], summary="Xác nhận cảnh báo vàng = cùng xe -> cho ra",
              response_model=ScanOut, responses={401: err[401], 404: err[404], 409: err[409]})
    def gate_confirm_near(body: ConfirmNearIn, user=Depends(current_user), svc=Depends(get_svc)):
        """Sau cảnh báo vàng (`WARN_NEAR`), bảo vệ xác nhận biển ở cổng ra và biển lúc vào là **cùng một xe**.
        Hệ thống check-out lượt `log_id` và ghi Audit Log (NEAR_CONFIRMED)."""
        open_log(svc, body.log_id)
        return _scan_out(svc.confirm_near_checkout(body.log_id, None, "MANUAL", user["user_id"], body.read_text))

    @app.get("/api/gate/lost-ticket/candidates", tags=["Cổng"], summary="Mất thẻ: danh sách xe để chọn",
             response_model=List[LostCandidateOut], responses={401: err[401]})
    def lost_candidates(plate: str = Query("", description="Biển đọc được tại cổng (có thể sai vài ký tự)",
                                           examples=["59O1-123.45"]),
                        show_all: bool = Query(False, description="True = trả về TẤT CẢ xe trong bãi"),
                        user=Depends(current_user), svc=Depends(get_svc)):
        """UC4 - Mất thẻ: trả về Top 5 xe có biển giống nhất (hoặc tất cả xe trong bãi) kèm giờ vào, để bảo vệ
        đối chiếu với ảnh lúc vào rồi chọn đúng lượt."""
        rows = svc.lost_candidates(normalize(plate), show_all)
        return [dict(log_id=l["id"], plate=l["plate_text"], slot=l["slot_code"], time_in=l["time_in"], distance=d)
                for d, l in rows]

    @app.post("/api/gate/lost-ticket", tags=["Cổng"], summary="Mất thẻ: cho xe ra và thu phí mất thẻ",
              response_model=ScanOut, responses={401: err[401], 404: err[404], 409: err[409]})
    def lost_checkout(body: LostCheckoutIn, user=Depends(current_user), svc=Depends(get_svc)):
        """Bảo vệ chọn đúng lượt vào (từ API candidates) -> check-out, thu phí mất thẻ cố định
        (`lost_ticket_fee`). Ghi Audit Log (LOST_TICKET)."""
        open_log(svc, body.log_id)
        return _scan_out(svc.checkout_lost(body.log_id, None, user["user_id"], body.read_text))

    # ================= Lượt gửi xe =================
    @app.get("/api/history", tags=["Lượt gửi xe"], summary="Lịch sử ra/vào (phân trang)", response_model=HistoryOut,
             responses={401: err[401]})
    def history(before: Optional[int] = Query(None, ge=1, description="Chỉ lấy lượt có id nhỏ hơn giá trị này "
                                                                     "(lấy từ `next_before` của trang trước)"),
                q: Optional[str] = Query(None, max_length=20, description="Tìm theo một phần biển số",
                                         examples=["59D1"]),
                size: int = Query(50, ge=1, le=200, description="Số dòng mỗi trang"),
                user=Depends(current_user), svc=Depends(get_svc)):
        """Phân trang **keyset** (nhanh và ổn định khi bảng rất lớn), mới nhất trước."""
        rows = svc.db.history_page(before, size, (q or "").strip() or None)
        return dict(items=rows, next_before=rows[-1]["id"] if len(rows) == size else None)

    @app.patch("/api/logs/{log_id}/plate", tags=["Lượt gửi xe"], summary="Sửa biển xe đang trong bãi",
               response_model=MessageOut, responses={401: err[401], 404: err[404], 409: err[409], 422: {"model": ErrorOut}})
    def fix_plate(log_id: int, body: PlateFixIn, user=Depends(current_user), svc=Depends(get_svc)):
        """Dùng khi máy đọc nhầm ký tự lúc check-in. Không cho trùng biển xe khác. Ghi Audit Log (PLATE_CORRECTED)."""
        key, text = need_plate(body.plate)
        open_log(svc, log_id)
        try:
            svc.correct_plate(log_id, text, key, user["user_id"])
        except ValueError as ex:
            fail(ex)
        return dict(message=f"Đã sửa biển thành {text}")

    @app.delete("/api/logs/{log_id}", tags=["Lượt gửi xe"], summary="Hủy lượt vào nhập nhầm",
                response_model=MessageOut, responses={401: err[401], 403: err[403], 404: err[404], 409: err[409]})
    def void_log(log_id: int, reason: str = Query("Nhập nhầm", max_length=100, description="Lý do hủy"),
                 user=Depends(current_user), svc=Depends(get_svc)):
        """Xóa lượt vào nhầm: ô trống lại, **không thu phí**. Bảo vệ chỉ hủy được trong `VOID_WINDOW_MIN` phút đầu
        (mặc định 10), Admin hủy bất cứ lúc nào. Ghi Audit Log (CHECKIN_VOIDED)."""
        open_log(svc, log_id)
        try:
            svc.void_checkin(log_id, user["user_id"], user["role"], reason)
        except (ValueError, PermissionError) as ex:
            fail(ex)
        return dict(message="Đã hủy lượt vào")

    # ================= Vé tháng (ADMIN) =================
    def sub_row(s: dict, today: date) -> dict:
        end = date.fromisoformat(s["end_date"])
        if s["start_date"] > today.isoformat():
            st, days = "NOT_STARTED", 0
        elif end < today:
            st, days = "EXPIRED", (today - end).days
        else:
            days = (end - today).days
            st = "SOON" if days <= config.SUB_WARN_DAYS else "ACTIVE"
        return dict(id=s["id"], plate=s["plate_text"], owner=s["owner_name"] or "", start_date=s["start_date"],
                    end_date=s["end_date"], status=st, days=days)

    @app.get("/api/subscriptions", tags=["Vé tháng"], summary="Danh sách vé tháng", response_model=List[SubOut],
             responses={401: err[401], 403: err[403]})
    def subs_list(user=Depends(admin_only), svc=Depends(get_svc)):
        """Kèm trạng thái: `ACTIVE` còn hạn, `SOON` sắp hết, `EXPIRED` hết hạn, `NOT_STARTED` chưa hiệu lực."""
        today = date.today()
        return [sub_row(s, today) for s in svc.db.list_subscriptions()]

    @app.post("/api/subscriptions", tags=["Vé tháng"], summary="Thêm vé tháng", status_code=201,
              response_model=SubOut, responses={400: err[400], 401: err[401], 403: err[403], 409: err[409],
                                                      422: {"model": ErrorOut}})
    def subs_add(body: SubIn, user=Depends(admin_only), svc=Depends(get_svc)):
        """Thêm vé tháng (whitelist - không tính phí khi còn hạn). Một biển không được có hai vé trùng khoảng ngày.
        Ghi Audit Log (SUB_ADDED)."""
        key, text = need_plate(body.plate)
        if body.end_date < body.start_date:
            raise HTTPException(400, "Ngày kết thúc phải sau ngày bắt đầu")
        try:
            svc.add_subscription(key, text, body.owner.strip(), body.start_date.isoformat(),
                                 body.end_date.isoformat(), user["user_id"])
        except ValueError as ex:
            fail(ex)
        s = svc.db.latest_subscription(key)
        return sub_row(s, date.today())

    @app.post("/api/subscriptions/{sub_id}/renew", tags=["Vé tháng"], summary="Gia hạn vé tháng +30 ngày",
              response_model=SubOut, responses={401: err[401], 403: err[403], 404: err[404]})
    def subs_renew(sub_id: int, user=Depends(admin_only), svc=Depends(get_svc)):
        """Tạo kỳ vé mới 30 ngày, bắt đầu hôm nay (nếu đã hết hạn) hoặc ngay sau ngày hết hạn cũ.
        Ghi Audit Log (SUB_RENEWED)."""
        sub = svc.db.get_subscription(sub_id)
        if not sub:
            raise HTTPException(404, "Vé không tồn tại")
        try:
            svc.renew_subscription(sub["plate_key"], user["user_id"])
        except ValueError as ex:
            fail(ex)
        return sub_row(svc.db.latest_subscription(sub["plate_key"]), date.today())

    @app.delete("/api/subscriptions/{sub_id}", tags=["Vé tháng"], summary="Xóa vé tháng", response_model=MessageOut,
                responses={401: err[401], 403: err[403], 404: err[404]})
    def subs_delete(sub_id: int, user=Depends(admin_only), svc=Depends(get_svc)):
        """Xóa một vé tháng. Ghi Audit Log (SUB_DELETED)."""
        if not svc.db.get_subscription(sub_id):
            raise HTTPException(404, "Vé không tồn tại")
        svc.delete_subscription(sub_id, user["user_id"])
        return dict(message="Đã xóa vé tháng")

    # ================= Báo cáo (ADMIN) =================
    @app.get("/api/stats/daily", tags=["Báo cáo"], summary="Thống kê xe vào/ra và doanh thu theo ngày",
             response_model=List[DailyStatOut], responses={400: err[400], 401: err[401], 403: err[403]})
    def stats_daily(date_from: Optional[date] = Query(None, alias="from", description="Mặc định: 6 ngày trước"),
                    date_to: Optional[date] = Query(None, alias="to", description="Mặc định: hôm nay"),
                    user=Depends(admin_only), svc=Depends(get_svc)):
        d2 = date_to or date.today()
        d1 = date_from or d2 - timedelta(days=6)
        if d1 > d2:
            raise HTTPException(400, "`from` phải nhỏ hơn hoặc bằng `to`")
        return svc.db.daily_stats(d1.isoformat(), d2.isoformat())

    @app.get("/api/stats/performance", tags=["Báo cáo"], summary="Hiệu năng cổng (NFR <= 1.5 s)",
             response_model=PerfOut, responses={401: err[401], 403: err[403], 404: err[404]})
    def stats_perf(user=Depends(admin_only), svc=Depends(get_svc)):
        """Trung vị / P95 thời gian xử lý mỗi lượt quét và % lượt đạt mục tiêu."""
        p = svc.db.perf_summary("GATE")
        if not p:
            raise HTTPException(404, "Chưa có số liệu hiệu năng")
        return dict(target_seconds=config.PERF_TARGET_S, n=p["n"], p50_ms=p["p50"], p95_ms=p["p95"],
                    avg_ms=p["avg"], max_ms=p["max"], ok_pct=p["ok_pct"])

    @app.get("/api/audit", tags=["Báo cáo"], summary="Audit Log (chỉ đọc)", response_model=AuditPageOut,
             responses={401: err[401], 403: err[403]})
    def audit(before: Optional[int] = Query(None, ge=1, description="Phân trang keyset: lấy sự kiện có id nhỏ hơn"),
              all_events: bool = Query(False, description="False = chỉ thao tác sửa tay / nhạy cảm; True = tất cả"),
              size: int = Query(50, ge=1, le=200),
              user=Depends(admin_only), svc=Depends(get_svc)):
        """Bảng `event_log` chỉ thêm, **không sửa/xóa được** (trigger SQLite). Mỗi dòng ghi ai, lúc nào, làm gì."""
        rows = svc.db.audit_page(before, size, None if all_events else AUDIT_SENSITIVE)
        items = [dict(r, action=EVENT_LABELS.get(r["event_type"], r["event_type"])) for r in rows]
        return dict(items=items, next_before=rows[-1]["id"] if len(rows) == size else None)

    @app.get("/api/export/revenue", tags=["Báo cáo"], summary="Xuất doanh thu ra Excel (.xlsx)",
             response_class=Response, responses={200: {"content": {XLSX: {}}, "description": "File .xlsx"},
                                                  400: err[400], 401: err[401], 403: err[403]})
    def export_revenue(date_from: date = Query(..., alias="from", examples=["2026-11-01"]),
                       date_to: date = Query(..., alias="to", examples=["2026-11-30"]),
                       user=Depends(admin_only), svc=Depends(get_svc)):
        """Hai sheet: doanh thu theo ngày và chi tiết từng lượt ra. Ghi Audit Log (EXPORT)."""
        import io
        if date_from > date_to:
            raise HTTPException(400, "`from` phải nhỏ hơn hoặc bằng `to`")
        svc.log_event("EXPORT", None, f"Xuất doanh thu {date_from} -> {date_to} (API)", user["user_id"])
        buf = io.BytesIO()
        svc.export_excel(date_from.isoformat(), date_to.isoformat(), buf)
        return Response(buf.getvalue(), media_type=XLSX, headers={
            "Content-Disposition": f'attachment; filename="doanh_thu_{date_from}_{date_to}.xlsx"'})

    @app.get("/api/export/audit", tags=["Báo cáo"], summary="Xuất Audit Log ra Excel (.xlsx)",
             response_class=Response, responses={200: {"content": {XLSX: {}}, "description": "File .xlsx"},
                                                  401: err[401], 403: err[403]})
    def export_audit(all_events: bool = Query(False, description="False = chỉ thao tác nhạy cảm"),
                     user=Depends(admin_only), svc=Depends(get_svc)):
        import io
        buf = io.BytesIO()
        svc.export_audit(buf, sensitive_only=not all_events)
        return Response(buf.getvalue(), media_type=XLSX,
                        headers={"Content-Disposition": 'attachment; filename="audit_log.xlsx"'})

    return app


# ======================================================================
# Chạy nền trong app Tkinter
# ======================================================================
def lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start(host=None, port=None, db_path=None, pipeline_getter=None):
    """Chạy API ở luồng nền. Trả về (server, địa chỉ Swagger UI). Cổng bận -> OSError."""
    host = host or config.API_HOST
    port = config.API_PORT if port is None else port
    cfg = uvicorn.Config(create_app(db_path, pipeline_getter), host=host, port=port, log_level="warning",
                         access_log=False)
    srv = uvicorn.Server(cfg)
    srv.install_signal_handlers = lambda: None                 # không ở luồng chính
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):                                       # chờ tối đa ~5 s cho máy chủ khởi động
        if srv.started:
            break
        if not th.is_alive():
            raise OSError(f"Không mở được cổng {port} (đang bận?)")
        time.sleep(0.05)
    real = srv.servers[0].sockets[0].getsockname()[1] if srv.servers else port
    shown = lan_ip() if host in ("0.0.0.0", "") else host
    return srv, f"http://{shown}:{real}/docs"


if __name__ == "__main__":                                     # python api_server.py  (chạy riêng, không cần giao diện)
    uvicorn.run(create_app(), host=config.API_HOST, port=config.API_PORT)
