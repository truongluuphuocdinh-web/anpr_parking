"""Lớp truy cập CSDL (SQLite). Gộp các Repository trong sơ đồ lớp vào một lớp Database cho gọn."""
import hashlib
import os
import sqlite3
from datetime import datetime

import config

FMT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  full_name TEXT,
  role TEXT NOT NULL CHECK(role IN ('GUARD','ADMIN')),
  is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS parking_slot(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  row_label TEXT NOT NULL CHECK(row_label IN ('A','B','C')),
  col_number INTEGER NOT NULL CHECK(col_number BETWEEN 1 AND 3),
  is_occupied INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS subscription(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plate_key TEXT NOT NULL, plate_text TEXT NOT NULL, owner_name TEXT,
  start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  created_by INTEGER REFERENCES app_user(id),
  CHECK(end_date >= start_date));
CREATE TABLE IF NOT EXISTS pricing_rule(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
  free_minutes INTEGER, block_minutes INTEGER, block_price INTEGER,
  daily_cap INTEGER, lost_ticket_fee INTEGER, is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS parking_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plate_key TEXT NOT NULL, plate_text TEXT NOT NULL,
  image_in TEXT, image_out TEXT,
  time_in TEXT NOT NULL, time_out TEXT,
  slot_id INTEGER REFERENCES parking_slot(id),
  status TEXT NOT NULL CHECK(status IN ('IN','OUT')),
  fee INTEGER NOT NULL DEFAULT 0,
  fee_type TEXT CHECK(fee_type IN ('SUBSCRIPTION','REGULAR','EXPIRED_SUB','LOST_TICKET')),
  in_method TEXT CHECK(in_method IN ('AUTO','MANUAL')),
  out_method TEXT CHECK(out_method IN ('AUTO','MANUAL')),
  guard_in_id INTEGER REFERENCES app_user(id),
  guard_out_id INTEGER REFERENCES app_user(id));
CREATE TABLE IF NOT EXISTS event_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_time TEXT NOT NULL, event_type TEXT NOT NULL,
  plate_text TEXT, detail TEXT,
  user_id INTEGER REFERENCES app_user(id),
  log_id INTEGER REFERENCES parking_log(id));
CREATE UNIQUE INDEX IF NOT EXISTS ux_log_open_plate ON parking_log(plate_key) WHERE status='IN';
CREATE UNIQUE INDEX IF NOT EXISTS ux_log_open_slot  ON parking_log(slot_id)   WHERE status='IN';
CREATE INDEX IF NOT EXISTS ix_log_status_plate ON parking_log(status, plate_key);
CREATE INDEX IF NOT EXISTS ix_log_time_in  ON parking_log(time_in);
CREATE INDEX IF NOT EXISTS ix_log_time_out ON parking_log(time_out);
CREATE INDEX IF NOT EXISTS ix_sub_plate ON subscription(plate_key, end_date);
CREATE INDEX IF NOT EXISTS ix_event_type ON event_log(event_type, id);
CREATE TABLE IF NOT EXISTS perf_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, ms REAL NOT NULL, ok INTEGER NOT NULL);
-- Audit log chỉ được THÊM: cấm xóa và cấm sửa nội dung (chỉ cho phép gỡ liên kết log_id khi hủy lượt vào nhầm)
CREATE TRIGGER IF NOT EXISTS trg_event_no_delete BEFORE DELETE ON event_log
BEGIN SELECT RAISE(ABORT, 'Audit log khong duoc xoa'); END;
CREATE TRIGGER IF NOT EXISTS trg_event_no_update
BEFORE UPDATE OF event_time, event_type, plate_text, detail, user_id ON event_log
BEGIN SELECT RAISE(ABORT, 'Audit log khong duoc sua'); END;
"""


def now_str() -> str:
    return datetime.now().strftime(FMT)


def hash_password(pw: str, salt: str = None) -> str:
    salt = salt or os.urandom(8).hex()
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    salt = stored.split("$", 1)[0]
    return hash_password(pw, salt) == stored


class Database:
    def __init__(self, path=config.DB_PATH, check_same_thread=True):
        # check_same_thread=False: REST API (api_server.py) tạo kết nối ở luồng này nhưng có thể dùng ở luồng khác;
        # mỗi yêu cầu có một kết nối riêng, dùng tuần tự nên vẫn an toàn.
        self.conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")            # app bảo vệ và web Admin cùng ghi 1 file -> chờ tối đa 5s
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.conn.executescript(SCHEMA)
        self._seed()

    # ---------- helpers ----------
    def _all(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def _seed(self):
        with self.conn:
            if not self._one("SELECT 1 FROM parking_slot LIMIT 1"):
                i = 1
                for r in config.SLOT_ROWS:
                    for c in range(1, config.SLOT_COLS + 1):
                        self.conn.execute("INSERT INTO parking_slot(id,code,row_label,col_number) VALUES(?,?,?,?)",
                                          (i, f"{r}{c}", r, c))
                        i += 1
            if not self._one("SELECT 1 FROM app_user LIMIT 1"):
                for u, p, n, role in config.DEFAULT_USERS:
                    self.conn.execute("INSERT INTO app_user(username,password_hash,full_name,role) VALUES(?,?,?,?)",
                                      (u, hash_password(p), n, role))
            if not self._one("SELECT 1 FROM pricing_rule LIMIT 1"):
                p = config.DEFAULT_PRICING
                self.conn.execute("INSERT INTO pricing_rule(name,free_minutes,block_minutes,block_price,daily_cap,lost_ticket_fee)"
                                  " VALUES(?,?,?,?,?,?)", (p["name"], p["free_minutes"], p["block_minutes"],
                                                           p["block_price"], p["daily_cap"], p["lost_ticket_fee"]))

    # ---------- user / pricing ----------
    def verify_user(self, username, password):
        u = self._one("SELECT * FROM app_user WHERE username=? AND is_active=1", (username,))
        return u if u and check_password(password, u["password_hash"]) else None

    def active_pricing(self):
        return self._one("SELECT * FROM pricing_rule WHERE is_active=1 ORDER BY id DESC LIMIT 1")

    # ---------- slot ----------
    def free_slots(self):
        return self._all("SELECT * FROM parking_slot WHERE is_occupied=0 ORDER BY id")

    def slot_by_code(self, code):
        return self._one("SELECT * FROM parking_slot WHERE code=?", (code.upper(),))

    # ---------- parking_log ----------
    _OPEN_SQL = ("SELECT l.*, s.code AS slot_code FROM parking_log l "
                 "LEFT JOIN parking_slot s ON s.id=l.slot_id WHERE l.status='IN'")

    def open_logs(self):
        return self._all(self._OPEN_SQL + " ORDER BY l.time_in")

    def get_log(self, log_id):
        return self._one("SELECT l.*, s.code AS slot_code FROM parking_log l "
                         "LEFT JOIN parking_slot s ON s.id=l.slot_id WHERE l.id=?", (log_id,))

    def insert_log(self, plate_key, plate_text, image_in, time_in, slot_id, in_method, guard_id):
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO parking_log(plate_key,plate_text,image_in,time_in,slot_id,status,in_method,guard_in_id)"
                " VALUES(?,?,?,?,?,'IN',?,?)",
                (plate_key, plate_text, image_in, time_in, slot_id, in_method, guard_id))
            self.conn.execute("UPDATE parking_slot SET is_occupied=1 WHERE id=?", (slot_id,))
        return cur.lastrowid

    def close_log(self, log_id, time_out, fee, fee_type, image_out, out_method, guard_id):
        with self.conn:
            log = self._one("SELECT slot_id FROM parking_log WHERE id=?", (log_id,))
            self.conn.execute(
                "UPDATE parking_log SET status='OUT', time_out=?, fee=?, fee_type=?, image_out=?, "
                "out_method=?, guard_out_id=? WHERE id=?",
                (time_out, fee, fee_type, image_out, out_method, guard_id, log_id))
            self.conn.execute("UPDATE parking_slot SET is_occupied=0 WHERE id=?", (log["slot_id"],))

    def move_log(self, log_id, new_slot_id):
        with self.conn:
            old = self._one("SELECT slot_id FROM parking_log WHERE id=?", (log_id,))["slot_id"]
            self.conn.execute("UPDATE parking_slot SET is_occupied=0 WHERE id=?", (old,))
            self.conn.execute("UPDATE parking_log SET slot_id=? WHERE id=?", (new_slot_id, log_id))
            self.conn.execute("UPDATE parking_slot SET is_occupied=1 WHERE id=?", (new_slot_id,))

    def update_log_plate(self, log_id, key, text):
        """Sửa biển của xe đang trong bãi (đọc nhầm)."""
        with self.conn:
            self.conn.execute("UPDATE parking_log SET plate_key=?, plate_text=? WHERE id=? AND status='IN'",
                              (key, text, log_id))

    def delete_open_log(self, log_id) -> bool:
        """Xóa lượt vào nhập nhầm (chỉ khi xe còn trong bãi), trả ô về trống. Sự kiện cũ giữ lại nhưng bỏ liên kết."""
        with self.conn:
            log = self._one("SELECT slot_id FROM parking_log WHERE id=? AND status='IN'", (log_id,))
            if not log:
                return False
            self.conn.execute("UPDATE event_log SET log_id=NULL WHERE log_id=?", (log_id,))
            self.conn.execute("DELETE FROM parking_log WHERE id=?", (log_id,))
            self.conn.execute("UPDATE parking_slot SET is_occupied=0 WHERE id=?", (log["slot_id"],))
        return True

    # ---------- subscription ----------
    def add_subscription(self, key, text, owner, start, end, created_by):
        if self._one("SELECT 1 FROM subscription WHERE plate_key=? AND end_date>=? AND start_date<=?",
                     (key, start, end)):
            raise ValueError("Biển số đã có vé tháng còn hạn trong khoảng thời gian này")
        with self.conn:
            self.conn.execute("INSERT INTO subscription(plate_key,plate_text,owner_name,start_date,end_date,created_by)"
                              " VALUES(?,?,?,?,?,?)", (key, text, owner, start, end, created_by))

    def delete_subscription(self, sub_id):
        with self.conn:
            self.conn.execute("DELETE FROM subscription WHERE id=?", (sub_id,))

    def list_subscriptions(self):
        return self._all("SELECT * FROM subscription ORDER BY end_date DESC, id DESC")

    def valid_subscription(self, key, day):
        return self._one("SELECT * FROM subscription WHERE plate_key=? AND start_date<=? AND end_date>=?",
                         (key, day, day))

    def latest_subscription(self, key):
        return self._one("SELECT * FROM subscription WHERE plate_key=? ORDER BY end_date DESC LIMIT 1", (key,))

    # ---------- event ----------
    def add_event(self, etype, plate=None, detail=None, user_id=None, log_id=None):
        with self.conn:
            self.conn.execute("INSERT INTO event_log(event_time,event_type,plate_text,detail,user_id,log_id)"
                              " VALUES(?,?,?,?,?,?)", (now_str(), etype, plate, detail, user_id, log_id))

    # ---------- báo cáo ----------
    def total_in_lot(self):
        return self._one("SELECT COUNT(*) AS n FROM parking_log WHERE status='IN'")["n"]

    def history_page(self, before_id=None, size=50, q=None):
        """Phân trang keyset: nhanh và ổn định khi bảng rất lớn. q = tìm theo một phần biển số."""
        qk = "%" + "".join(ch for ch in (q or "").upper() if ch.isalnum()) + "%" if q else None
        return self._all(
            "SELECT l.*, s.code AS slot_code FROM parking_log l LEFT JOIN parking_slot s ON s.id=l.slot_id "
            "WHERE (? IS NULL OR l.id < ?) AND (? IS NULL OR l.plate_key LIKE ?) ORDER BY l.id DESC LIMIT ?",
            (before_id, before_id, qk, qk, size))

    def daily_stats(self, d_from, d_to):
        ins = {r["d"]: r["n"] for r in self._all(
            "SELECT date(time_in) AS d, COUNT(*) AS n FROM parking_log "
            "WHERE date(time_in) BETWEEN ? AND ? GROUP BY d", (d_from, d_to))}
        outs = {r["d"]: r for r in self._all(
            "SELECT date(time_out) AS d, COUNT(*) AS n, COALESCE(SUM(fee),0) AS rev FROM parking_log "
            "WHERE status='OUT' AND date(time_out) BETWEEN ? AND ? GROUP BY d", (d_from, d_to))}
        rows = []
        for d in sorted(set(ins) | set(outs)):
            rows.append(dict(day=d, vao=ins.get(d, 0), ra=outs[d]["n"] if d in outs else 0,
                             doanh_thu=outs[d]["rev"] if d in outs else 0))
        return rows

    def logs_out_between(self, d_from, d_to):
        return self._all(
            "SELECT l.*, s.code AS slot_code FROM parking_log l LEFT JOIN parking_slot s ON s.id=l.slot_id "
            "WHERE l.status='OUT' AND date(l.time_out) BETWEEN ? AND ? ORDER BY l.time_out", (d_from, d_to))

    # ---------- audit log (nhật ký kiểm toán) ----------
    def audit_page(self, before_id=None, size=50, types=None):
        """Trang nhật ký, mới nhất trước, kèm tên người thao tác. types = danh sách loại sự kiện cần lọc."""
        types = list(types) if types else None
        marks = ",".join("?" * len(types)) if types else ""
        sql = ("SELECT e.id, e.event_time, e.event_type, e.plate_text, e.detail, e.user_id, "
               "u.username, u.role FROM event_log e LEFT JOIN app_user u ON u.id=e.user_id "
               "WHERE (? IS NULL OR e.id < ?)" + (f" AND e.event_type IN ({marks})" if types else "") +
               " ORDER BY e.id DESC LIMIT ?")
        return self._all(sql, (before_id, before_id, *(types or []), size))

    def audit_between(self, types=None, limit=100000):
        types = list(types) if types else None
        marks = ",".join("?" * len(types)) if types else ""
        sql = ("SELECT e.id, e.event_time, e.event_type, e.plate_text, e.detail, u.username, u.role "
               "FROM event_log e LEFT JOIN app_user u ON u.id=e.user_id" +
               (f" WHERE e.event_type IN ({marks})" if types else "") + " ORDER BY e.id DESC LIMIT ?")
        return self._all(sql, (*(types or []), limit))

    # ---------- hiệu năng (NFR: nhận diện + mở barrier <= 1.5 s) ----------
    def record_perf(self, kind, ms, target_s):
        with self.conn:
            self.conn.execute("INSERT INTO perf_log(ts,kind,ms,ok) VALUES(?,?,?,?)",
                              (now_str(), kind, float(ms), 1 if ms <= target_s * 1000 else 0))

    def perf_summary(self, kind="GATE", last=1000):
        rows = [r["ms"] for r in self._all("SELECT ms FROM perf_log WHERE kind=? ORDER BY id DESC LIMIT ?",
                                           (kind, last))]
        if not rows:
            return None
        rows.sort()
        pick = lambda p: rows[min(len(rows) - 1, int(round(p * (len(rows) - 1))))]
        ok = self._one("SELECT SUM(ok) AS k FROM (SELECT ok FROM perf_log WHERE kind=? ORDER BY id DESC LIMIT ?)",
                       (kind, last))["k"] or 0
        return dict(n=len(rows), p50=pick(0.5), p95=pick(0.95), avg=sum(rows) / len(rows), max=rows[-1],
                    ok_pct=100.0 * ok / len(rows))

    # ---------- tài khoản / vé tháng ----------
    def set_password(self, user_id, new_password):
        with self.conn:
            self.conn.execute("UPDATE app_user SET password_hash=? WHERE id=?", (hash_password(new_password), user_id))

    def get_user(self, user_id):
        return self._one("SELECT id, username, full_name, role FROM app_user WHERE id=?", (user_id,))

    def get_subscription(self, sub_id):
        return self._one("SELECT * FROM subscription WHERE id=?", (sub_id,))
