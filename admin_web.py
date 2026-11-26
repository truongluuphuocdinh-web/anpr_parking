"""Web Admin cho MÁY THỨ HAI trong cùng mạng LAN (không cần cài thêm thư viện, chỉ dùng thư viện chuẩn của Python).

Máy bảo vệ chạy app như bình thường (camera, quét biển, barrier) và đồng thời mở một máy chủ web nhỏ.
Máy Admin mở trình duyệt vào  http://<IP máy bảo vệ>:8000  rồi đăng nhập tài khoản ADMIN để:
  xem bãi realtime, lịch sử ra/vào, quản lý vé tháng, xem Audit Log, xuất Excel, đổi mật khẩu.
Hai máy dùng CHUNG một file CSDL (SQLite) nằm ở máy bảo vệ -> không bị lệch dữ liệu.

Bảo mật (chỉ dùng trong mạng nội bộ, KHÔNG đưa ra internet - đường truyền không mã hóa HTTPS):
  - chỉ tài khoản vai trò ADMIN; phiên đăng nhập có hạn 30 phút không thao tác; cookie HttpOnly + SameSite=Strict
  - mọi thao tác ghi đều kiểm tra mã CSRF; mọi dữ liệu hiển thị đều được escape (chống XSS); SQL dùng tham số
  - sai mật khẩu quá 5 lần / 5 phút từ một địa chỉ -> tạm khóa; mọi thao tác ghi đều vào Audit Log
"""
import hmac
import html
import io
import secrets
import socket
import threading
import time
from datetime import date, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import config
from db import Database
from plate_utils import is_valid, normalize, pretty
from services import AUDIT_SENSITIVE, EVENT_LABELS, ParkingService

SESSION_TTL = 30 * 60
MAX_FAILS, FAIL_WINDOW = 5, 300
h = html.escape

CSS = """
*{box-sizing:border-box}body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f3f5f8;color:#222}
header{background:#1d3557;color:#fff;padding:10px 18px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header a{color:#cfe3ff;text-decoration:none;padding:4px 8px;border-radius:4px}header a.on{background:#457b9d;color:#fff}
header .sp{flex:1}main{padding:16px 18px;max-width:1200px;margin:auto}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}.card{background:#fff;border-radius:8px;padding:12px 16px;
min-width:150px;box-shadow:0 1px 3px #0002}.card b{display:block;font-size:24px;color:#1d3557}
table{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px #0002}
th,td{padding:7px 10px;border-bottom:1px solid #e6e9ee;text-align:left;font-size:14px}th{background:#e9eef5}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-width:620px;margin-bottom:16px}
.cell{border-radius:8px;padding:10px;text-align:center;min-height:74px;font-size:14px;border:1px solid #0001}
.free{background:#d8f0d8}.occ{background:#f4c7c3}.sub{background:#c9e4ff}.soon{background:#ffe0a3}.exp{background:#ff9e9e}
.exp-row td{background:#ffe1e1}.soon-row td{background:#fff2cc}
form.inline{display:inline}input,select{padding:6px 8px;border:1px solid #bbb;border-radius:5px}
button{padding:6px 12px;border:0;border-radius:5px;background:#457b9d;color:#fff;cursor:pointer}button.red{background:#c0392b}
.msg{padding:8px 12px;border-radius:6px;margin-bottom:10px}.ok{background:#d6f5d6}.err{background:#ffd6d6}.warn{background:#fff2cc}
.pager{margin:10px 0}.pager a{margin-right:12px}h2{margin:6px 0 12px}.sm{color:#666;font-size:13px}
"""


def lan_ip():
    """Địa chỉ IP của máy này trong mạng LAN (không gửi gói tin nào ra ngoài)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, db_path):
        super().__init__(addr, Handler)
        self.db_path = db_path
        self.sessions, self.fails, self.lock = {}, {}, threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "ParkingAdmin"

    def log_message(self, *a):
        pass

    # ---------- tiện ích ----------
    def _send(self, body, status=200, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to, cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _session(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        sid = c["sid"].value if "sid" in c else ""
        with self.server.lock:
            s = self.server.sessions.get(sid)
            if s and s["exp"] > time.time():
                s["exp"] = time.time() + SESSION_TTL
                return sid, s
            self.server.sessions.pop(sid, None)
        return "", None

    def _form(self):
        n = min(int(self.headers.get("Content-Length") or 0), 20000)
        return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}

    def _svc(self):
        db = Database(self.server.db_path)                    # mỗi yêu cầu một kết nối (SQLite không dùng chung giữa luồng)
        self._db = db
        return ParkingService(db)

    def _layout(self, title, body, sess, active=""):
        nav = [("/", "Tổng quan"), ("/history", "Lịch sử ra/vào"), ("/subs", "Vé tháng"), ("/audit", "Audit Log"),
               ("/password", "Đổi mật khẩu")]
        links = "".join(f'<a class="{"on" if p == active else ""}" href="{p}">{t}</a>' for p, t in nav)
        out = (f'<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" '
               f'value="{h(sess["csrf"])}"><button>Đăng xuất ({h(sess["username"])})</button></form>')
        return (f'<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width,initial-scale=1"><title>{h(title)} - Quản lý bãi xe</title>'
                f'<style>{CSS}</style></head><body><header><b>BÃI XE - ADMIN</b>{links}<span class="sp"></span>{out}'
                f'</header><main>{body}</main></body></html>')

    def _need_login(self):
        sid, s = self._session()
        if not s:
            self._redirect("/login")
            return None
        return s

    def _csrf_ok(self, sess, form):
        return hmac.compare_digest(form.get("csrf", ""), sess["csrf"])

    # ---------- định tuyến ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/login":
                return self._login_page()
            sess = self._need_login()
            if not sess:
                return
            routes = {"/": self.p_dashboard, "/history": self.p_history, "/subs": self.p_subs,
                      "/audit": self.p_audit, "/password": self.p_password, "/export": self.p_export,
                      "/export_audit": self.p_export_audit}
            fn = routes.get(u.path)
            if not fn:
                return self._send(self._layout("Không tìm thấy", "<h2>Không tìm thấy trang</h2>", sess), 404)
            fn(sess, q)
        finally:
            if getattr(self, "_db", None):
                self._db.conn.close()

    def do_POST(self):
        u = urlparse(self.path)
        try:
            form = self._form()
            if u.path == "/login":
                return self._login_post(form)
            sess = self._need_login()
            if not sess:
                return
            if not self._csrf_ok(sess, form):
                return self._send(self._layout("Lỗi", '<div class="msg err">Phiên không hợp lệ (CSRF). Hãy tải lại trang.'
                                               "</div>", sess), 403)
            routes = {"/logout": self.a_logout, "/subs/add": self.a_sub_add, "/subs/renew": self.a_sub_renew,
                      "/subs/delete": self.a_sub_delete, "/password": self.a_password}
            fn = routes.get(u.path)
            if not fn:
                return self._send("Không tìm thấy", 404)
            fn(sess, form)
        finally:
            if getattr(self, "_db", None):
                self._db.conn.close()

    # ---------- đăng nhập ----------
    def _login_page(self, err=""):
        e = f'<div class="msg err">{h(err)}</div>' if err else ""
        self._send(f'<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content='
                   f'"width=device-width,initial-scale=1"><title>Đăng nhập</title><style>{CSS}</style></head><body>'
                   f'<main style="max-width:360px;margin-top:80px"><h2>Đăng nhập Admin</h2>{e}'
                   f'<form method="post" action="/login"><p><input name="username" placeholder="Tài khoản" '
                   f'style="width:100%" autofocus></p><p><input name="password" type="password" '
                   f'placeholder="Mật khẩu" style="width:100%"></p><button style="width:100%">Đăng nhập</button>'
                   f'</form></main></body></html>', 401 if err else 200)

    def _login_post(self, form):
        ip, now = self.client_address[0], time.time()
        with self.server.lock:
            recent = [t for t in self.server.fails.get(ip, []) if now - t < FAIL_WINDOW]
            self.server.fails[ip] = recent
        if len(recent) >= MAX_FAILS:
            return self._login_page("Sai quá nhiều lần. Vui lòng thử lại sau 5 phút.")
        svc = self._svc()
        user = svc.login(form.get("username", "").strip(), form.get("password", ""))
        if not user or user["role"] != "ADMIN":
            with self.server.lock:
                self.server.fails.setdefault(ip, []).append(now)
            if user:                                           # đúng mật khẩu nhưng không phải Admin
                svc.log_event("LOGIN_FAIL", None, f"{user['username']} không phải ADMIN (web, {ip})", user["id"])
            return self._login_page("Sai tài khoản/mật khẩu hoặc không phải tài khoản Admin")
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.server.lock:
            self.server.sessions[sid] = dict(user_id=user["id"], username=user["username"], csrf=csrf,
                                             exp=now + SESSION_TTL)
            self.server.fails.pop(ip, None)
        svc.log_event("LOGIN", None, f"{user['username']} (web, {ip})", user["id"])
        self._redirect("/", f"sid={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}")

    def a_logout(self, sess, form):
        sid, _ = self._session()
        with self.server.lock:
            self.server.sessions.pop(sid, None)
        self._redirect("/login", "sid=; Max-Age=0; Path=/")

    # ---------- trang ----------
    @staticmethod
    def _flash(q):
        m = q.get("msg", "")
        if not m:
            return ""
        return f'<div class="msg {"err" if q.get("e") else "ok"}">{h(m)}</div>'

    def p_dashboard(self, sess, q):
        svc = self._svc()
        db, today = svc.db, date.today()
        stats = {r["day"]: r for r in db.daily_stats((today - timedelta(days=6)).isoformat(), today.isoformat())}
        t = stats.get(today.isoformat(), dict(vao=0, ra=0, doanh_thu=0))
        occ = {l["slot_code"]: l for l in db.open_logs()}
        total = len(config.SLOT_ROWS) * config.SLOT_COLS
        cells = ""
        for r in config.SLOT_ROWS:
            for c in range(1, config.SLOT_COLS + 1):
                code, log = f"{r}{c}", occ.get(f"{r}{c}")
                if not log:
                    cells += f'<div class="cell free"><b>{code}</b><br>trống</div>'
                    continue
                info = svc.subscription_status(log["plate_key"])
                cls = {"ACTIVE": "soon" if info.soon else "sub", "EXPIRED": "exp", "NOT_STARTED": "exp"}.get(info.status, "occ")
                tag = {"ACTIVE": "VÉ THÁNG" + (" (sắp hết)" if info.soon else ""), "EXPIRED": "VÉ THÁNG HẾT HẠN",
                       "NOT_STARTED": "VÉ THÁNG CHƯA HIỆU LỰC"}.get(info.status, "Vé lẻ")
                cells += (f'<div class="cell {cls}"><b>{code}</b><br>{h(log["plate_text"])}<br>'
                          f'<span class="sm">{h(tag)}<br>vào {h(log["time_in"][11:16])}</span></div>')
        p = db.perf_summary("GATE")
        perf = (f'<div class="card">Hiệu năng cổng<b>{p["p50"] / 1000:.2f} s</b><span class="sm">trung vị · P95 '
                f'{p["p95"] / 1000:.2f} s · đạt ≤{config.PERF_TARGET_S}s: {p["ok_pct"]:.0f}% ({p["n"]} lượt)</span></div>'
                if p else '<div class="card">Hiệu năng cổng<b>—</b><span class="sm">chưa có số liệu</span></div>')
        default_pw = any(u == sess["username"] and db.verify_user(u, pw) for u, pw, *_ in config.DEFAULT_USERS)
        warn = ('<div class="msg warn">Bạn đang dùng <b>mật khẩu mặc định</b>. Hãy <a href="/password">đổi mật khẩu</a> '
                'trước khi dùng thật.</div>') if default_pw else ""
        rows = "".join(f'<tr><td>{h(d)}</td><td>{r["vao"]}</td><td>{r["ra"]}</td><td>{r["doanh_thu"]:,}</td></tr>'
                       for d, r in sorted(stats.items(), reverse=True))
        body = (f'<meta http-equiv="refresh" content="5">{warn}{self._flash(q)}<h2>Tổng quan (tự làm mới mỗi 5 giây)</h2>'
                f'<div class="cards"><div class="card">Xe trong bãi<b>{len(occ)}/{total}</b></div>'
                f'<div class="card">Hôm nay: vào<b>{t["vao"]}</b></div><div class="card">Hôm nay: ra<b>{t["ra"]}</b></div>'
                f'<div class="card">Doanh thu hôm nay<b>{t["doanh_thu"]:,} đ</b></div>{perf}</div>'
                f'<h3>Sơ đồ bãi</h3><div class="grid">{cells}</div><h3>7 ngày gần nhất</h3>'
                f'<table><tr><th>Ngày</th><th>Xe vào</th><th>Xe ra</th><th>Doanh thu (đ)</th></tr>{rows}</table>'
                f'<h3>Xuất báo cáo Excel</h3><form method="get" action="/export">Từ <input name="from" '
                f'value="{today.replace(day=1).isoformat()}" size="11"> đến <input name="to" value="{today.isoformat()}" '
                f'size="11"> <button>Tải doanh thu .xlsx</button></form>')
        self._send(self._layout("Tổng quan", body, sess, "/"))

    def p_history(self, sess, q):
        svc = self._svc()
        before = int(q["before"]) if q.get("before", "").isdigit() else None
        search = q.get("q", "").strip()[:20]
        rows = svc.db.history_page(before, 50, search or None)
        tr = "".join(
            f'<tr><td>{r["id"]}</td><td>{h(r["plate_text"])}</td><td>{h(r["time_in"])}</td><td>{h(r["time_out"] or "")}</td>'
            f'<td>{h(r["slot_code"] or "")}</td><td>{r["fee"]:,}</td><td>{h(r["fee_type"] or "")}</td>'
            f'<td>{h(r["status"])}</td></tr>' for r in rows)
        more = (f'<a href="/history?before={rows[-1]["id"]}&q={quote(search)}">Cũ hơn &gt;</a>'
                if len(rows) == 50 else "")
        body = (f'<h2>Lịch sử ra/vào</h2><form method="get" action="/history">Tìm biển: <input name="q" '
                f'value="{h(search)}"> <button>Tìm</button></form><div class="pager">{more}</div>'
                f'<table><tr><th>ID</th><th>Biển số</th><th>Giờ vào</th><th>Giờ ra</th><th>Ô</th><th>Phí</th>'
                f'<th>Loại phí</th><th>Trạng thái</th></tr>{tr}</table>')
        self._send(self._layout("Lịch sử", body, sess, "/history"))

    def p_subs(self, sess, q):
        svc, today = self._svc(), date.today()
        rows = ""
        for s in svc.db.list_subscriptions():
            end = date.fromisoformat(s["end_date"])
            if s["start_date"] > today.isoformat():
                st, cls = "Chưa bắt đầu", "soon-row"
            elif end < today:
                st, cls = f"HẾT HẠN ({(today - end).days} ngày)", "exp-row"
            else:
                left = (end - today).days
                st, cls = f"Còn {left} ngày" + (" - sắp hết" if left <= config.SUB_WARN_DAYS else ""), \
                          ("soon-row" if left <= config.SUB_WARN_DAYS else "")
            f = lambda act, label, cl="": (f'<form class="inline" method="post" action="/subs/{act}">'
                                           f'<input type="hidden" name="csrf" value="{h(sess["csrf"])}">'
                                           f'<input type="hidden" name="id" value="{s["id"]}"><button class="{cl}">{label}'
                                           f'</button></form>')
            rows += (f'<tr class="{cls}"><td>{s["id"]}</td><td>{h(s["plate_text"])}</td><td>{h(s["owner_name"] or "")}</td>'
                     f'<td>{h(s["start_date"])}</td><td>{h(s["end_date"])}</td><td>{h(st)}</td>'
                     f'<td>{f("renew", "Gia hạn +30 ngày")} {f("delete", "Xóa", "red")}</td></tr>')
        body = (f'<h2>Vé tháng</h2>{self._flash(q)}<form method="post" action="/subs/add"><input type="hidden" '
                f'name="csrf" value="{h(sess["csrf"])}"><input name="plate" placeholder="Biển số" size="12"> '
                f'<input name="owner" placeholder="Chủ xe" size="16"> Từ <input name="start" value="{today.isoformat()}" '
                f'size="11"> đến <input name="end" value="{(today + timedelta(days=30)).isoformat()}" size="11"> '
                f'<button>Thêm vé</button></form><br><table><tr><th>ID</th><th>Biển số</th><th>Chủ xe</th><th>Từ</th>'
                f'<th>Đến</th><th>Trạng thái</th><th></th></tr>{rows}</table>')
        self._send(self._layout("Vé tháng", body, sess, "/subs"))

    def p_audit(self, sess, q):
        svc = self._svc()
        before = int(q["before"]) if q.get("before", "").isdigit() else None
        all_events = q.get("t") == "all"
        rows = svc.db.audit_page(before, 50, None if all_events else AUDIT_SENSITIVE)
        tr = "".join(
            f'<tr><td>{r["id"]}</td><td>{h(r["event_time"])}</td><td>{h(r["username"] or "(hệ thống)")}'
            f'{" (" + h(r["role"]) + ")" if r["role"] else ""}</td><td>{h(EVENT_LABELS.get(r["event_type"], r["event_type"]))}'
            f'</td><td>{h(r["plate_text"] or "")}</td><td>{h(r["detail"] or "")}</td></tr>' for r in rows)
        t = "all" if all_events else "s"
        more = (f'<a href="/audit?t={t}&before={rows[-1]["id"]}">Cũ hơn &gt;</a>' if len(rows) == 50 else "")
        other = ('<a href="/audit?t=s">Chỉ thao tác sửa tay / nhạy cảm</a>' if all_events else
                 '<a href="/audit?t=all">Xem tất cả sự kiện</a>')
        body = (f'<h2>Audit Log (chỉ đọc - không sửa/xóa được)</h2><div class="pager">{other} · {more} · '
                f'<a href="/export_audit?t={t}">Tải Excel</a></div><table><tr><th>ID</th><th>Thời gian</th>'
                f'<th>Người thao tác</th><th>Hành động</th><th>Biển số</th><th>Chi tiết</th></tr>{tr}</table>')
        self._send(self._layout("Audit Log", body, sess, "/audit"))

    def p_password(self, sess, q):
        body = (f'<h2>Đổi mật khẩu</h2>{self._flash(q)}<form method="post" action="/password"><input type="hidden" '
                f'name="csrf" value="{h(sess["csrf"])}"><p><input name="old" type="password" placeholder="Mật khẩu hiện tại">'
                f'</p><p><input name="new" type="password" placeholder="Mật khẩu mới (>= 6 ký tự)"></p><p>'
                f'<input name="new2" type="password" placeholder="Nhập lại mật khẩu mới"></p><button>Đổi mật khẩu</button>'
                f'</form>')
        self._send(self._layout("Đổi mật khẩu", body, sess, "/password"))

    # ---------- xuất file ----------
    def _xlsx(self, name, writer):
        buf = io.BytesIO()
        writer(buf)
        self._send(buf.getvalue(), 200,
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    def p_export(self, sess, q):
        try:
            d1, d2 = date.fromisoformat(q.get("from", "")), date.fromisoformat(q.get("to", ""))
        except ValueError:
            return self._redirect("/?e=1&msg=" + quote("Ngày không hợp lệ (định dạng YYYY-MM-DD)"))
        svc = self._svc()
        svc.log_event("EXPORT", None, f"Xuất doanh thu {d1} -> {d2} (web)", sess["user_id"])
        self._xlsx(f"doanh_thu_{d1}_{d2}.xlsx", lambda b: svc.export_excel(d1.isoformat(), d2.isoformat(), b))

    def p_export_audit(self, sess, q):
        svc = self._svc()
        self._xlsx("audit_log.xlsx", lambda b: svc.export_audit(b, sensitive_only=q.get("t") != "all"))

    # ---------- thao tác ghi (đều đi qua service -> có Audit Log) ----------
    def a_sub_add(self, sess, f):
        svc, key = self._svc(), normalize(f.get("plate", ""))
        try:
            if not is_valid(key):
                raise ValueError("Biển số không hợp lệ (ví dụ 59D1-123.45)")
            start, end = date.fromisoformat(f.get("start", "")), date.fromisoformat(f.get("end", ""))
            if end < start:
                raise ValueError("Ngày kết thúc phải sau ngày bắt đầu")
            svc.add_subscription(key, pretty(key), f.get("owner", "").strip()[:60], start.isoformat(), end.isoformat(),
                                 sess["user_id"])
            msg, e = f"Đã thêm vé tháng cho {pretty(key)}", ""
        except ValueError as ex:
            msg, e = str(ex), "1"
        self._redirect(f"/subs?msg={quote(msg)}" + ("&e=1" if e else ""))

    def a_sub_renew(self, sess, f):
        svc = self._svc()
        try:
            sub = svc.db.get_subscription(int(f.get("id", "0") or 0))
            if not sub:
                raise ValueError("Vé không tồn tại")
            start, end = svc.renew_subscription(sub["plate_key"], sess["user_id"])
            msg, e = f"Đã gia hạn {sub['plate_text']}: {start} -> {end}", ""
        except ValueError as ex:
            msg, e = str(ex), "1"
        self._redirect(f"/subs?msg={quote(msg)}" + ("&e=1" if e else ""))

    def a_sub_delete(self, sess, f):
        svc = self._svc()
        try:
            svc.delete_subscription(int(f.get("id", "0") or 0), sess["user_id"])
            msg, e = "Đã xóa vé tháng", ""
        except ValueError as ex:
            msg, e = str(ex), "1"
        self._redirect(f"/subs?msg={quote(msg)}" + ("&e=1" if e else ""))

    def a_password(self, sess, f):
        svc = self._svc()
        try:
            if f.get("new") != f.get("new2"):
                raise ValueError("Hai lần nhập mật khẩu mới không khớp")
            svc.change_password(sess["user_id"], f.get("old", ""), f.get("new", ""))
            msg, e = "Đã đổi mật khẩu", ""
        except ValueError as ex:
            msg, e = str(ex), "1"
        self._redirect(f"/password?msg={quote(msg)}" + ("&e=1" if e else ""))


def start(host=None, port=None, db_path=None):
    """Chạy web Admin ở luồng nền. Trả về (server, địa chỉ để máy khác truy cập)."""
    host, port = host or config.ADMIN_WEB_HOST, config.ADMIN_WEB_PORT if port is None else port
    srv = AdminServer((host, port), str(db_path or config.DB_PATH))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    shown = lan_ip() if host in ("0.0.0.0", "") else host
    return srv, f"http://{shown}:{srv.server_address[1]}"
