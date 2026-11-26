"""Lớp nghiệp vụ: đối chiếu biển, check-in/out, tính phí, ô đỗ, tìm xe, báo cáo."""
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import config
from db import Database, FMT
from plate_utils import levenshtein


def parse_dt(s: str) -> datetime:
    return datetime.strptime(s, FMT)


class FeeService:
    @staticmethod
    def calculate(minutes: int, rule: dict) -> int:
        """Miễn phí N phút đầu; sau đó tính theo khối (làm tròn lên), trần theo ngày."""
        if minutes <= rule["free_minutes"]:
            return 0
        blocks = math.ceil(minutes / rule["block_minutes"])
        days = max(1, math.ceil(minutes / 1440))
        return min(blocks * rule["block_price"], rule["daily_cap"] * days)


@dataclass
class MatchResult:
    level: str                      # EXACT | NEAR | NONE
    log: Optional[dict] = None
    distance: int = 0


def _dmy(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


EVENT_LABELS = {
    "LOGIN": "Đăng nhập", "LOGIN_FAIL": "Đăng nhập SAI", "PASSWORD_CHANGED": "Đổi mật khẩu",
    "MANUAL_ENTRY": "Bảo vệ nhập biển bằng tay", "OCR_FAIL": "Máy không đọc được -> nhập tay",
    "OCR_CORRECTED": "Bảo vệ sửa biển máy đọc", "PLATE_CORRECTED": "Sửa biển xe đang trong bãi",
    "CHECKIN_VOIDED": "Hủy lượt vào nhầm", "NEAR_CONFIRMED": "Xác nhận biển gần giống = cùng xe",
    "NEAR_REJECTED": "Xác nhận biển gần giống = xe khác", "YELLOW_WARNING": "Cảnh báo vàng (biển gần giống)",
    "LOST_TICKET": "Xử lý mất thẻ", "SLOT_MOVED": "Di dời xe sang ô khác", "LOT_FULL": "Bãi đầy",
    "SUB_ADDED": "Thêm vé tháng", "SUB_RENEWED": "Gia hạn vé tháng", "SUB_DELETED": "Xóa vé tháng",
    "SUB_EXPIRED": "Vé tháng hết hạn", "EXPORT": "Xuất báo cáo",
}
# Nhóm thao tác "sửa tay / nhạy cảm" mà NFR bảo mật bắt buộc phải có Audit Log
AUDIT_SENSITIVE = ["MANUAL_ENTRY", "OCR_FAIL", "OCR_CORRECTED", "PLATE_CORRECTED", "CHECKIN_VOIDED", "NEAR_CONFIRMED",
                   "NEAR_REJECTED", "LOST_TICKET", "SLOT_MOVED", "SUB_ADDED", "SUB_RENEWED", "SUB_DELETED",
                   "PASSWORD_CHANGED", "LOGIN_FAIL"]


@dataclass
class SubInfo:
    """Tình trạng vé tháng của một biển số."""
    status: str = "NONE"            # ACTIVE | EXPIRED | NOT_STARTED | NONE
    ref_date: str = ""              # ACTIVE/EXPIRED: ngày hết hạn; NOT_STARTED: ngày bắt đầu (YYYY-MM-DD)
    days: int = 0                   # ACTIVE: còn mấy ngày; EXPIRED: đã hết hạn mấy ngày
    owner: str = ""

    @property
    def soon(self) -> bool:
        return self.status == "ACTIVE" and self.days <= config.SUB_WARN_DAYS

    @property
    def label(self) -> str:
        if self.status == "ACTIVE":
            base = f"VÉ THÁNG còn {self.days} ngày (đến {_dmy(self.ref_date)})"
            return base + " - SẮP HẾT HẠN" if self.soon else base
        if self.status == "EXPIRED":
            return f"VÉ THÁNG HẾT HẠN ngày {_dmy(self.ref_date)} ({self.days} ngày trước)"
        if self.status == "NOT_STARTED":
            return f"VÉ THÁNG chưa có hiệu lực (từ {_dmy(self.ref_date)})"
        return "Vé lẻ"

    @property
    def short(self) -> str:
        """Nhãn không dấu để vẽ lên video (OpenCV không vẽ được chữ có dấu)."""
        if self.status == "ACTIVE":
            return f"VE THANG con {self.days} ngay" + (" - SAP HET HAN" if self.soon else "")
        if self.status == "EXPIRED":
            return f"VE THANG HET HAN {_dmy(self.ref_date)}"
        if self.status == "NOT_STARTED":
            return "VE THANG CHUA HIEU LUC"
        return "VE LE"


@dataclass
class ScanOutcome:
    action: str                     # CHECK_IN | CHECK_OUT | WARN_NEAR | LOT_FULL
    message: str
    plate: str = ""
    slot: str = ""
    fee: int = 0
    fee_type: str = ""
    minutes: int = 0
    near_log: Optional[dict] = None
    distance: int = 0
    log_id: int = 0
    sub: Optional[SubInfo] = None    # tình trạng vé tháng (để giao diện cảnh báo)


class ParkingService:
    def __init__(self, db: Database, clock=datetime.now, rng=None):
        self.db, self.clock, self.rng = db, clock, rng or random.Random()

    # ---- xác thực ----
    def login(self, username, password):
        u = self.db.verify_user(username, password)
        if not u:
            self.log_event("LOGIN_FAIL", None, f"Tài khoản thử: {username[:40]}")
        return u

    def change_password(self, user_id, old, new):
        u = self.db.get_user(user_id)
        if not u or not self.db.verify_user(u["username"], old):
            raise ValueError("Mật khẩu hiện tại không đúng")
        if len(new) < 6:
            raise ValueError("Mật khẩu mới phải có ít nhất 6 ký tự")
        self.db.set_password(user_id, new)
        self.log_event("PASSWORD_CHANGED", None, u["username"], user_id)

    def audit_manual(self, user_id, machine_text, machine_conf, final_text, reason):
        """NFR bảo mật: mọi lần bảo vệ nhập/sửa biển bằng tay đều lưu Audit Log (máy đọc gì -> bảo vệ sửa thành gì)."""
        before = f"Máy đọc '{machine_text}' ({int(machine_conf * 100)}%)" if machine_text else "Máy không đọc được"
        etype = "OCR_CORRECTED" if machine_text and machine_conf > 0 else "MANUAL_ENTRY"
        self.log_event(etype, final_text, f"{before} -> bảo vệ nhập '{final_text}'. Lý do: {reason}", user_id)

    def record_perf(self, kind, ms):
        self.db.record_perf(kind, ms, config.PERF_TARGET_S)

    def log_event(self, etype, plate=None, detail=None, user_id=None, log_id=None):
        self.db.add_event(etype, plate, detail, user_id, log_id)

    # ---- vé tháng (Admin) - mọi thao tác đều ghi Audit Log ----
    def add_subscription(self, key, text, owner, start, end, user_id=None):
        self.db.add_subscription(key, text, owner, start, end, user_id)
        self.log_event("SUB_ADDED", text, f"Chủ xe '{owner}', {start} -> {end}", user_id)

    def delete_subscription(self, sub_id, user_id=None):
        sub = self.db.get_subscription(sub_id)
        if not sub:
            raise ValueError("Vé không tồn tại")
        self.db.delete_subscription(sub_id)
        self.log_event("SUB_DELETED", sub["plate_text"], f"Chủ xe '{sub['owner_name']}', {sub['start_date']} -> "
                       f"{sub['end_date']}", user_id)

    def renew_subscription(self, key, user_id=None, days=30):
        """Gia hạn: vé mới bắt đầu hôm nay (nếu đã hết hạn) hoặc ngay sau ngày hết hạn cũ."""
        last = self.db.latest_subscription(key)
        if not last:
            raise ValueError("Biển này chưa có vé tháng")
        start = max(self.clock().date(), date.fromisoformat(last["end_date"]) + timedelta(days=1))
        end = start + timedelta(days=days)
        self.db.add_subscription(key, last["plate_text"], last["owner_name"], start.isoformat(), end.isoformat(),
                                 user_id)
        self.log_event("SUB_RENEWED", last["plate_text"], f"{start} -> {end}", user_id)
        return start, end

    # ---- vé tháng: tra cứu ----
    def subscription_status(self, key: str) -> SubInfo:
        today = self.clock().date()
        t = today.isoformat()
        act = self.db.valid_subscription(key, t)
        if act:
            return SubInfo("ACTIVE", act["end_date"], (date.fromisoformat(act["end_date"]) - today).days,
                           act["owner_name"] or "")
        last = self.db.latest_subscription(key)
        if not last:
            return SubInfo("NONE")
        if last["start_date"] > t:
            return SubInfo("NOT_STARTED", last["start_date"], 0, last["owner_name"] or "")
        return SubInfo("EXPIRED", last["end_date"], (today - date.fromisoformat(last["end_date"])).days,
                       last["owner_name"] or "")

    # ---- đối chiếu ----
    def match_open(self, key: str) -> MatchResult:
        best = MatchResult("NONE")
        for log in self.db.open_logs():
            d = levenshtein(key, log["plate_key"])
            if d == 0:
                return MatchResult("EXACT", log, 0)
            if d <= config.NEAR_DISTANCE and (best.level == "NONE" or d < best.distance):
                best = MatchResult("NEAR", log, d)
        return best

    def top_similar(self, key: str, n: int = config.TOP_N):
        rows = [(levenshtein(key, l["plate_key"]), l) for l in self.db.open_logs()]
        rows.sort(key=lambda r: (r[0], r[1]["time_in"]))
        return rows[:n]

    def lost_candidates(self, key: str, show_all: bool = False):
        """UC4: danh sách xe để bảo vệ chọn - Top N giống nhất, hoặc TẤT CẢ xe trong bãi (tìm theo ảnh/giờ vào)."""
        return self.top_similar(key, 10 ** 6 if show_all else config.TOP_N)

    # ---- luồng chính: quét biển ----
    def process_scan(self, text, key, image_path=None, method="AUTO", user_id=None) -> ScanOutcome:
        m = self.match_open(key)
        if m.level == "EXACT":
            return self.check_out(m.log, image_path, method, user_id)
        if m.level == "NEAR":
            self.log_event("YELLOW_WARNING", text, f"In {m.log['plate_text']} / Out {text} / d={m.distance}",
                           user_id, m.log["id"])
            return ScanOutcome("WARN_NEAR", f"Biển {text} gần giống {m.log['plate_text']} đang trong bãi",
                               plate=text, near_log=m.log, distance=m.distance)
        return self.check_in(text, key, image_path, method, user_id)

    def check_in(self, text, key, image_path=None, method="AUTO", user_id=None) -> ScanOutcome:
        free = self.db.free_slots()
        if not free:
            self.log_event("LOT_FULL", text, None, user_id)
            return ScanOutcome("LOT_FULL", "Bãi đầy, không thể cho xe vào", plate=text)
        slot = self.rng.choice(free)
        now = self.clock()
        log_id = self.db.insert_log(key, text, image_path, now.strftime(FMT), slot["id"], method, user_id)
        info = self.subscription_status(key)
        msg = f"CHECK-IN {text} -> ô {slot['code']} | {info.label}"
        if info.status in ("EXPIRED", "NOT_STARTED"):        # vé hết hạn: vẫn cho vào nhưng cảnh báo, sẽ thu phí vé lẻ khi ra
            msg += " -> sẽ thu phí vé lẻ khi ra"
            self.log_event("SUB_EXPIRED", text, f"Vào bãi: {info.label}", user_id, log_id)
        return ScanOutcome("CHECK_IN", msg, plate=text, slot=slot["code"],
                           fee_type="SUBSCRIPTION" if info.status == "ACTIVE" else "", log_id=log_id, sub=info)

    def check_out(self, log: dict, image_path=None, method="AUTO", user_id=None) -> ScanOutcome:
        now = self.clock()
        minutes = max(0, math.ceil((now - parse_dt(log["time_in"])).total_seconds() / 60))
        key = log["plate_key"]
        info = self.subscription_status(key)
        if info.status == "ACTIVE":
            fee, ftype, note = 0, "SUBSCRIPTION", "Vé tháng - miễn phí" + (" (SẮP HẾT HẠN: còn %d ngày)" % info.days if info.soon else "")
        else:
            fee = FeeService.calculate(minutes, self.db.active_pricing())
            if info.status in ("EXPIRED", "NOT_STARTED"):
                ftype, note = "EXPIRED_SUB", f"{info.label} - thu phí vé lẻ"
                self.log_event("SUB_EXPIRED", log["plate_text"], note, user_id, log["id"])
            else:
                ftype, note = "REGULAR", "Vé lẻ"
        self.db.close_log(log["id"], now.strftime(FMT), fee, ftype, image_path, method, user_id)
        return ScanOutcome("CHECK_OUT", f"CHECK-OUT {log['plate_text']} | {minutes} phút | {fee:,} đ | {note}",
                           plate=log["plate_text"], slot=log["slot_code"], fee=fee, fee_type=ftype,
                           minutes=minutes, log_id=log["id"], sub=info)

    def confirm_near_checkout(self, log_id, image_path=None, method="AUTO", user_id=None, read_text="") -> ScanOutcome:
        """Bảo vệ xác nhận biển gần giống là cùng xe (cảnh báo vàng) -> cho ra. Quyết định của bảo vệ được ghi Audit Log."""
        log = self.db.get_log(log_id)
        self.log_event("NEAR_CONFIRMED", read_text or log["plate_text"],
                       f"Biển ra đọc '{read_text}' khác biển vào '{log['plate_text']}' - bảo vệ xác nhận CÙNG XE",
                       user_id, log_id)
        return self.check_out(log, image_path, method, user_id)

    def reject_near(self, text, near_log, user_id=None):
        self.log_event("NEAR_REJECTED", text, f"Biển '{text}' gần giống '{near_log['plate_text']}' - bảo vệ xác nhận "
                       "XE KHÁC (cho vào)", user_id, near_log["id"])

    def checkout_lost(self, log_id, image_path=None, user_id=None, read_text="") -> ScanOutcome:
        """UC04: mất thẻ / không khớp -> bảo vệ chọn lượt In đúng, thu phí mất thẻ."""
        log = self.db.get_log(log_id)
        now = self.clock()
        minutes = max(0, math.ceil((now - parse_dt(log["time_in"])).total_seconds() / 60))
        fee = self.db.active_pricing()["lost_ticket_fee"]
        self.db.close_log(log_id, now.strftime(FMT), fee, "LOST_TICKET", image_path, "MANUAL", user_id)
        self.log_event("LOST_TICKET", log["plate_text"],
                       f"Biển đọc tại cổng '{read_text}' -> bảo vệ chọn xe '{log['plate_text']}' (ô {log['slot_code']}); "
                       f"phí mất thẻ {fee:,}", user_id, log_id)
        return ScanOutcome("CHECK_OUT", f"CHECK-OUT (mất thẻ) {log['plate_text']} | {fee:,} đ",
                           plate=log["plate_text"], slot=log["slot_code"], fee=fee,
                           fee_type="LOST_TICKET", minutes=minutes, log_id=log_id)

    # ---- sửa lỗi: đọc nhầm biển ----
    def correct_plate(self, log_id, new_text, new_key, user_id=None):
        """Sửa biển của xe ĐANG trong bãi (máy đọc sai ký tự). Mọi lần sửa đều được ghi sự kiện."""
        log = self.db.get_log(log_id)
        if not log or log["status"] != "IN":
            raise ValueError("Lượt này không còn trong bãi nên không sửa được")
        if new_key == log["plate_key"]:
            raise ValueError("Biển mới giống biển cũ")
        dup = next((l for l in self.db.open_logs() if l["plate_key"] == new_key and l["id"] != log_id), None)
        if dup:
            raise ValueError(f"Biển {dup['plate_text']} đã có xe trong ô {dup['slot_code']}")
        self.db.update_log_plate(log_id, new_key, new_text)
        self.log_event("PLATE_CORRECTED", new_text, f"{log['plate_text']} -> {new_text} (ô {log['slot_code']})",
                       user_id, log_id)

    def can_void(self, log, role) -> bool:
        if role == "ADMIN":
            return True
        minutes = (self.clock() - parse_dt(log["time_in"])).total_seconds() / 60
        return minutes <= config.VOID_WINDOW_MIN

    def void_checkin(self, log_id, user_id=None, role="GUARD", reason="Nhập nhầm"):
        """Hủy lượt vào nhập nhầm (không thu phí, ô trống lại). Bảo vệ chỉ được trong VOID_WINDOW_MIN phút đầu;
        Admin bất cứ lúc nào. Luôn ghi sự kiện CHECKIN_VOIDED để đối soát."""
        log = self.db.get_log(log_id)
        if not log or log["status"] != "IN":
            raise ValueError("Lượt này không còn trong bãi nên không hủy được")
        if not self.can_void(log, role):
            raise PermissionError(f"Đã quá {config.VOID_WINDOW_MIN} phút kể từ lúc vào. Hãy nhờ Admin hủy, "
                                  "hoặc cho xe ra bình thường.")
        self.db.delete_open_log(log_id)
        self.log_event("CHECKIN_VOIDED", log["plate_text"],
                       f"{reason}; ô {log['slot_code']}; vào {log['time_in']}", user_id, None)

    # ---- tìm xe & di dời ----
    def find_car(self, key: str) -> MatchResult:
        return self.match_open(key)

    def move_car(self, src_code: str, dst_code: str, user_id=None):
        src, dst = self.db.slot_by_code(src_code), self.db.slot_by_code(dst_code)
        if not src or not dst:
            raise ValueError("Ô không tồn tại")
        log = next((l for l in self.db.open_logs() if l["slot_id"] == src["id"]), None)
        if not log:
            raise ValueError(f"Ô {src['code']} đang trống, không có xe để di dời")
        if dst["is_occupied"]:
            raise ValueError(f"Ô {dst['code']} đã có xe")
        self.db.move_log(log["id"], dst["id"])
        self.log_event("SLOT_MOVED", log["plate_text"], f"{src['code']} -> {dst['code']}", user_id, log["id"])

    # ---- báo cáo ----
    def export_excel(self, d_from: str, d_to: str, path) -> str:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Doanh thu theo ngày"
        ws.append(["Ngày", "Xe vào", "Xe ra", "Doanh thu (đ)"])
        stats = self.db.daily_stats(d_from, d_to)
        for r in stats:
            ws.append([r["day"], r["vao"], r["ra"], r["doanh_thu"]])
        ws.append(["TỔNG", sum(r["vao"] for r in stats), sum(r["ra"] for r in stats),
                   sum(r["doanh_thu"] for r in stats)])
        ws2 = wb.create_sheet("Chi tiết lượt ra")
        ws2.append(["Biển số", "Giờ vào", "Giờ ra", "Ô", "Phí (đ)", "Loại phí"])
        for l in self.db.logs_out_between(d_from, d_to):
            ws2.append([l["plate_text"], l["time_in"], l["time_out"], l["slot_code"], l["fee"], l["fee_type"]])
        for sheet in (ws, ws2):
            for col in sheet.columns:
                sheet.column_dimensions[col[0].column_letter].width = 20
        wb.save(path if hasattr(path, "write") else str(path))
        return path if hasattr(path, "write") else str(path)

    def export_audit(self, path, sensitive_only=False):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Audit log"
        ws.append(["ID", "Thời gian", "Người thao tác", "Vai trò", "Hành động", "Biển số", "Chi tiết"])
        for r in self.db.audit_between(AUDIT_SENSITIVE if sensitive_only else None):
            ws.append([r["id"], r["event_time"], r["username"] or "(hệ thống)", r["role"] or "",
                       EVENT_LABELS.get(r["event_type"], r["event_type"]), r["plate_text"] or "", r["detail"] or ""])
        for col, w in zip("ABCDEFG", (8, 20, 16, 10, 36, 14, 90)):
            ws.column_dimensions[col].width = w
        wb.save(path if hasattr(path, "write") else str(path))
        return path if hasattr(path, "write") else str(path)
