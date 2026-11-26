"""Giao diện Tkinter: màn hình Bảo vệ (cổng + ma trận 3x3) và các tab Admin."""
import queue
import threading
import time
import tkinter as tk
from datetime import date, timedelta
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

import config
from anpr import AnprPipeline, PlateResult, save_image
import admin_web
import api_server
import camera
import theme
from db import Database
from live import LiveRecognizer, draw_overlay
from plate_utils import is_valid, normalize, pretty
from services import AUDIT_SENSITIVE, EVENT_LABELS, ParkingService

COLORS = {"free": "#e8f5e9", "occ": "#fde3e1", "find": "#ffe680", "sel": "#a9c9f5",
          "sub": "#dcecfb", "soon": "#ffedb8", "exp": "#ffb4b4"}      # trống / có xe / vé tháng / sắp hết hạn / hết hạn
BADGE_BGR = {"ACTIVE": (200, 110, 20), "SOON": (0, 140, 255), "EXPIRED": (50, 50, 215), "NOT_STARTED": (50, 50, 215),
             "NONE": (120, 120, 120)}
STATUS_COLORS = {"CHECK_IN": "#2e7d32", "CHECK_OUT": "#1565c0", "WARN": "#b8860b", "ERR": "#c62828", "INFO": "#555555"}
CELL_FONT = ("Segoe UI", 10, "bold")


def make_tree(parent, cols, widths, height=14):
    tree = ttk.Treeview(parent, columns=cols, show="headings", height=height)
    for c, w in zip(cols, widths):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="center")
    tree.pack(fill="both", expand=True, padx=6, pady=6)
    return tree


def thumb(src, size=(300, 200)):
    """Ảnh thu nhỏ cho Tkinter từ đường dẫn file hoặc mảng OpenCV. Không có/hỏng -> None."""
    try:
        if isinstance(src, str):
            img = Image.open(src)
        else:
            img = Image.fromarray(cv2.cvtColor(src, cv2.COLOR_BGR2RGB))
        img.thumbnail(size)
        return ImageTk.PhotoImage(img)
    except Exception:
        return None


# =====================================================================
#                        TAB BẢO VỆ (CỔNG)
# =====================================================================
class GuardTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app, self.svc, self.user = app, app.svc, app.user
        self.cap = camera.CameraStream()                     # camera USB hoặc điện thoại, đọc ở luồng nền
        self.live, self.cam_q, self._cam_lost = False, queue.Queue(), False
        self.frame, self.photo = None, None
        self.busy, self.q = False, queue.Queue()
        self.liveai = LiveRecognizer(lambda: self.app.pipeline)      # nhận diện trực tiếp chạy nền
        self.show_ai, self._ver = tk.BooleanVar(value=True), -1
        self._badge_cache = (None, 0.0, None)
        self._m = None                                       # phép đo thời gian: nhận diện -> mở barrier (NFR <= 1.5 s)
        self.move_src, self.highlight = None, None
        self.move_mode = tk.BooleanVar(value=False)
        self._build()
        self.refresh_matrix()
        self._tick()
        self.use_camera(first=True)                          # mặc định mở camera USB của máy tính

    CAM_LABELS = ["Camera USB (máy tính) - số 0", "Camera USB (máy tính) - số 1", "Camera điện thoại (IP)"]
    PHONE_HELP = ("Mở app camera IP trên điện thoại (IP Webcam / DroidCam...), bấm Start server rồi nhập địa chỉ.\n"
                  "Điện thoại và máy tính phải cùng mạng Wi-Fi.\n\n"
                  "Ví dụ:  192.168.1.5:8080   (IP Webcam)\n"
                  "            192.168.1.5:4747   (DroidCam)\n"
                  "            rtsp://192.168.1.5:8554/live   (app dùng RTSP)")

    # ---------- dựng giao diện ----------
    def _build(self):
        W = theme.WHITE
        # --- hàng thẻ số liệu ---
        kpis = tk.Frame(self, bg=theme.BG)
        kpis.pack(fill="x", padx=14, pady=(14, 8))
        self.kpi = {}
        for key, title, color in (("in", "XE TRONG BÃI", theme.ACCENT), ("free", "CÒN TRỐNG", theme.GREEN),
                                  ("vao", "XE VÀO HÔM NAY", "#8e24aa"), ("rev", "DOANH THU HÔM NAY", "#ef6c00")):
            outer, val, _sub = theme.stat_card(kpis, title, color)
            outer.pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.kpi[key] = val
        body = tk.Frame(self, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        left = tk.Frame(body, bg=theme.BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, bg=theme.BG)
        right.pack(side="right", fill="y", padx=(12, 0))

        # --- camera ---
        cam_card, cam_body = theme.card(left, "CAMERA GIÁM SÁT")
        cam_card.pack(fill="x")
        self.cam = tk.Label(cam_body, text="Không có camera - hãy bấm 'Mở ảnh' để thử", bg="#111827", fg="#9ca3af",
                            width=64, height=20, font=theme.font(11))
        self.cam.pack()
        bar = tk.Frame(cam_body, bg=W)
        bar.pack(fill="x", pady=(12, 4))
        ttk.Button(bar, text="QUÉT BIỂN (Vào/Ra)", style="Accent.TButton",
                   command=lambda: self.start("scan")).pack(side="left", padx=(0, 8))
        for text, cmd in (("Tìm xe", lambda: self.start("find")), ("Nhập biển tay", self.manual),
                          ("Mất thẻ / Top 5", lambda: self.start("lost"))):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=4)
        bar2 = tk.Frame(cam_body, bg=W)
        bar2.pack(fill="x", pady=(6, 0))
        self.cam_source = tk.StringVar(value=self.CAM_LABELS[0])
        ttk.Combobox(bar2, textvariable=self.cam_source, values=self.CAM_LABELS, state="readonly",
                     width=28).pack(side="left", padx=(0, 6))
        ttk.Button(bar2, text="Dùng camera", command=self.use_camera).pack(side="left", padx=3)
        ttk.Button(bar2, text="Địa chỉ ĐT...", command=self.edit_phone_url).pack(side="left", padx=3)
        ttk.Button(bar2, text="Mở ảnh", command=self.load_image).pack(side="left", padx=3)
        ttk.Checkbutton(cam_body, text="Hiện khung nhận diện trực tiếp trên video", variable=self.show_ai,
                        style="Card.TCheckbutton", command=self._toggle_ai).pack(anchor="w", pady=(8, 0))

        # --- kết quả gần nhất ---
        res_card, res = theme.card(right, "KẾT QUẢ GẦN NHẤT")
        res_card.pack(fill="x")
        self.banner = tk.Label(res, text="SẴN SÀNG", bg=theme.SLATE, fg="white", font=theme.font(18, True), pady=6)
        self.banner.pack(fill="x")
        plate_frame, self.plate_lbl = theme.plate_box(res)
        plate_frame.pack(pady=8)
        self.fee_lbl = tk.Label(res, text="", bg=W, fg=theme.ACCENT_DK, font=theme.font(15, True))
        self.fee_lbl.pack()
        self.stat = tk.Label(res, text="Sẵn sàng", wraplength=400, justify="left", font=theme.font(10), bg=W,
                             fg=theme.TEXT, anchor="nw", height=3)
        self.stat.pack(fill="x", pady=(4, 6))
        self.barrier = tk.Label(res, text="BARRIER: ĐÓNG", bg=theme.RED, fg="white", font=theme.font(12, True), pady=6)
        self.barrier.pack(fill="x")

        # --- sơ đồ bãi ---
        grid_card, grid = theme.card(right, "SƠ ĐỒ BÃI XE (bấm chuột phải vào xe để sửa/hủy)")
        grid_card.pack(fill="x", pady=(12, 0))
        holder = tk.Frame(grid, bg=W)
        holder.pack()
        self.cells = {}
        for ri, r in enumerate(config.SLOT_ROWS):
            for c in range(1, config.SLOT_COLS + 1):
                code = f"{r}{c}"
                lbl = tk.Label(holder, text=code, width=14, height=3, relief="flat", bd=0, font=CELL_FONT,
                               highlightthickness=1, highlightbackground="#cfd8dc", fg=theme.TEXT)
                lbl.grid(row=ri, column=c - 1, padx=3, pady=3)
                lbl.bind("<Button-1>", lambda e, cc=code: self.on_cell(cc))
                lbl.bind("<Button-3>", lambda e, cc=code: self.fix_dialog(cc))     # chuột phải vào ô có xe -> sửa/hủy
                self.cells[code] = lbl
        ttk.Checkbutton(grid, text="Chế độ di dời xe (bấm ô có xe, rồi bấm ô trống)", style="Card.TCheckbutton",
                        variable=self.move_mode, command=self._reset_move).pack(anchor="w", pady=(8, 2))
        ttk.Button(grid, text="Sửa biển / Hủy lượt vào nhập nhầm...", command=self.fix_dialog).pack(anchor="w", pady=2)

        # --- nhật ký (dưới camera; UI_COMPACT = True thì ẩn đi cho vừa màn hình thấp) ---
        log_card, log = theme.card(left, "NHẬT KÝ HOẠT ĐỘNG")
        if not config.UI_COMPACT:
            log_card.pack(fill="x", pady=(12, 0))
        self.listbox = tk.Listbox(log, height=4, width=58, relief="flat", bd=0, highlightthickness=0,
                                  font=theme.font(9), activestyle="none", selectbackground="#dbeafe",
                                  selectforeground=theme.TEXT)
        self.listbox.pack(fill="both", expand=True)

    def set_status(self, text, kind="INFO"):
        """Thông báo dưới dạng banner màu + dòng chi tiết + nhật ký. Thông tin phụ (INFO) không đè lên kết quả xe."""
        self.stat.configure(text=text)
        if kind != "INFO":
            label, color = theme.BANNER[kind]
            self.banner.configure(text=label, bg=color)
        self.listbox.insert(0, f"{time.strftime('%H:%M:%S')}  {text.replace(chr(10), ' | ')}")
        try:
            self.listbox.itemconfig(0, fg=STATUS_COLORS[kind])
        except tk.TclError:
            pass
        if self.listbox.size() > 200:
            self.listbox.delete(200, "end")

    def set_plate(self, plate, fee_text=""):
        self.plate_lbl.configure(text=plate or "- - -")
        self.fee_lbl.configure(text=fee_text)

    def beep(self):
        def _b():
            try:
                import winsound
                winsound.Beep(1200, 350)
            except Exception:
                pass
        threading.Thread(target=_b, daemon=True).start()          # không chặn giao diện -> không cộng vào thời gian xử lý

    # ---------- camera / ảnh ----------
    def _tick(self):
        if self.live:
            lost = self.cap.stale(3.0)                       # >3 giây không có hình (rớt Wi-Fi / rút USB)
            if lost != self._cam_lost:
                self._cam_lost = lost
                self.set_status("Mất kết nối camera - đang thử nối lại..." if lost else "Camera đã có hình trở lại",
                                "ERR" if lost else "INFO")
            ok, f = self.cap.read()
            if ok:
                self.frame = f
                if self.show_ai.get():
                    self.liveai.submit(f)                    # luồng nền tự lấy khung mới nhất
                    st = self.liveai.snapshot(max_age=1.0)
                    f = draw_overlay(f, st, self._badge(st))                 # khung + chữ nhỏ + huy hiệu vé tháng
                self._show(f)
        elif self.frame is not None and self.liveai.version != self._ver:     # ảnh tĩnh: vẽ lại khi có kết quả mới
            self._ver = self.liveai.version
            st = self.liveai.snapshot()
            self._show(draw_overlay(self.frame, st, self._badge(st)) if self.show_ai.get() else self.frame)
        self.after(40, self._tick)

    def _badge(self, state):
        """Huy hiệu vé tháng dưới khung biển (chỉ khi biển đã đọc chắc). Có nhớ tạm 2 giây để khỏi truy vấn liên tục."""
        if state is None or not state.key or not state.stable:
            return None
        now = time.time()
        if self._badge_cache[0] == state.key and now - self._badge_cache[1] < 2:
            return self._badge_cache[2]
        info = self.svc.subscription_status(state.key)
        kind = "SOON" if info.soon else info.status
        badge = (info.short, BADGE_BGR[kind])
        self._badge_cache = (state.key, now, badge)
        return badge

    def _toggle_ai(self):
        self.liveai.enabled = self.show_ai.get()
        if not self.live and self.frame is not None:         # ảnh tĩnh: bật lại thì nhận diện lại, tắt thì bỏ khung
            if self.show_ai.get():
                self.liveai.reset()
                self.liveai.submit(self.frame)
            self._ver = -1

    def _show(self, frame):
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        img.thumbnail((540, 360))
        self.photo = ImageTk.PhotoImage(img)
        self.cam.configure(image=self.photo, text="", width=img.width, height=img.height)

    def load_image(self):
        path = filedialog.askopenfilename(filetypes=[("Ảnh", "*.png *.jpg *.jpeg *.bmp")])
        if not path:
            return
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            messagebox.showerror("Lỗi", "Không đọc được ảnh")
            return
        self.live, self.frame = False, img
        self.liveai.min_votes = 1                            # ảnh tĩnh chỉ có 1 khung -> 1 phiếu là đủ
        self.liveai.reset()
        self._ver = -1
        self._show(img)
        if self.show_ai.get():
            self.liveai.submit(img)

    def edit_phone_url(self):
        cur = camera.load_phone_url()
        raw = simpledialog.askstring("Camera điện thoại", self.PHONE_HELP + "\n\nĐịa chỉ:", initialvalue=cur,
                                     parent=self)
        if not raw:
            return None
        url = camera.normalize_url(raw)
        camera.save_phone_url(url)
        self.cam_source.set(self.CAM_LABELS[2])
        self.use_camera()
        return url

    def use_camera(self, first=False):
        """Mở camera đang chọn ở luồng nền (mở camera/mạng có thể mất vài giây -> không làm đơ giao diện)."""
        label = self.cam_source.get()
        if label == self.CAM_LABELS[2]:
            src = camera.load_phone_url()
            if not src:
                src = self.edit_phone_url()                  # chưa có địa chỉ -> hỏi (edit_phone_url sẽ tự mở lại)
                return
        else:
            src = int(label[-1])
        if not first:
            self.set_status(f"Đang kết nối: {label}...", "INFO")

        def work():
            self.cam_q.put((self.cap.open(src), label, src))

        threading.Thread(target=work, daemon=True).start()
        self.after(150, self._poll_cam, first)

    def _poll_cam(self, first):
        try:
            ok, label, src = self.cam_q.get_nowait()
        except queue.Empty:
            self.after(150, self._poll_cam, first)
            return
        if ok:
            self.live, self._cam_lost = True, False
            self.liveai.min_votes = 2                        # camera: cần >=2 khung hình đọc giống nhau mới coi là chắc
            self.liveai.reset()
            self.set_status(f"Đã kết nối: {label}", "INFO")
        elif not first:                                      # lúc mới mở app mà không có camera thì im lặng
            hint = ("\n\n" + self.PHONE_HELP) if label == self.CAM_LABELS[2] else \
                   "\n\nThử chọn 'số 1', hoặc kiểm tra camera có đang bị app khác dùng không."
            messagebox.showwarning("Camera", f"Không mở được: {label}\n({src}){hint}")

    # ---------- nhận diện (chạy nền để không đơ giao diện) ----------
    def start(self, mode):
        if self.busy:
            return
        if self.frame is None:
            messagebox.showwarning("Chưa có ảnh", "Chưa có hình từ camera. Hãy mở ảnh hoặc bật camera.")
            return
        if self.app.pipeline is None:
            messagebox.showinfo("Vui lòng chờ", "Mô hình AI đang được tải...")
            return
        t0 = time.perf_counter()                             # bắt đầu đo: từ lúc bấm QUÉT
        frame = self.frame.copy()
        key = self.liveai.stable_key() if (self.live and self.show_ai.get()) else None
        if key:                                              # biển đã được nhiều khung hình xác nhận -> dùng luôn
            st = self.liveai.snapshot()
            self._m = {"t0": t0, "base": st.ms, "auto": True}       # cộng thêm thời gian nhận diện của khung đó
            return self._after_recognize(mode, frame, PlateResult(text=st.text, key=key, det_conf=1.0,
                                                                   ocr_conf=st.conf, box=st.box, valid=True))
        self._m = {"t0": t0, "base": 0.0, "auto": True}
        self.busy = True
        self.set_status("Đang nhận diện biển số...", "INFO")

        def work():
            try:
                res = self.app.pipeline.recognize(frame)
            except Exception as e:
                res = PlateResult(message=f"Lỗi nhận diện: {e}")
            self.q.put((mode, frame, res))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self):
        try:
            mode, frame, res = self.q.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        self.busy = False
        self._after_recognize(mode, frame, res)

    def _ask_plate(self, message, initial=""):
        raw = simpledialog.askstring("Nhập biển số", message, initialvalue=initial, parent=self)
        if not raw:
            return None, None
        key = normalize(raw)
        if not is_valid(key):
            messagebox.showerror("Sai định dạng", f"'{raw}' không phải biển số hợp lệ (ví dụ 59D1-123.45)")
            return None, None
        return pretty(key), key

    def _after_recognize(self, mode, frame, res):
        text, key, method = res.text, res.key, "AUTO"
        if not res.valid:                                    # luồng ngoại lệ: còi + nhập tay
            self.beep()
            text, key = self._ask_plate(f"{res.message}\nBảo vệ hãy nhập biển số bằng tay:", res.text)
            if not key:
                self.set_status("Đã hủy - chưa xử lý xe", "ERR")
                return
            method = "MANUAL"
            if self._m:
                self._m["auto"] = False                       # có người can thiệp -> không tính vào chỉ tiêu 1.5 s
            self.svc.audit_manual(self.user["id"], res.text, res.ocr_conf, text, res.message or "đọc sai định dạng")
        elif res.ocr_conf < config.AUTO_ACCEPT_CONF:           # đúng định dạng nhưng máy chưa chắc -> hỏi bảo vệ
            self.beep()
            text, key = self._ask_plate(f"Máy đọc: {res.text}  (độ tin cậy {int(res.ocr_conf * 100)}%)\n"
                                        "Hãy đối chiếu với biển thật, SỬA nếu sai rồi bấm OK:", res.text)
            if not key:
                self.set_status("Đã hủy - chưa xử lý xe", "ERR")
                return
            if self._m:
                self._m["auto"] = False
            if key != res.key:                                  # bảo vệ sửa lại -> ghi Audit Log (máy đọc gì -> sửa thành gì)
                method = "MANUAL"
                self.svc.audit_manual(self.user["id"], res.text, res.ocr_conf, text,
                                      f"máy chưa chắc (< {int(config.AUTO_ACCEPT_CONF * 100)}%)")
        self.dispatch(mode, frame, text, key, method)

    def manual(self):
        text, key = self._ask_plate("Nhập biển số:")
        if key:
            self._m = None
            self.svc.audit_manual(self.user["id"], "", 0, text, "bảo vệ bấm nút Nhập tay")
            self.dispatch("scan", self.frame, text, key, "MANUAL")

    # ---------- xử lý theo chế độ ----------
    def dispatch(self, mode, frame, text, key, method):
        self.set_plate(text)
        if mode == "find":
            return self.do_find(text, key)
        if mode == "lost":
            return self.do_lost(text, key, frame)
        img = save_image(frame, "scan", key) if frame is not None else None
        o = self.svc.process_scan(text, key, img, method, self.user["id"])
        if o.action == "WARN_NEAR":
            self.beep()
            return self.yellow_dialog(o, key, img, method)
        self.show_outcome(o)

    def show_outcome(self, o):
        kind = {"CHECK_IN": "CHECK_IN", "CHECK_OUT": "CHECK_OUT"}.get(o.action, "ERR")
        sub = o.sub
        expired = bool(sub and sub.status in ("EXPIRED", "NOT_STARTED"))
        if o.action in ("CHECK_IN", "CHECK_OUT") and sub and (expired or sub.soon):
            kind = "WARN"                                     # vé tháng hết hạn / sắp hết hạn -> màu vàng + còi
        passed = o.action in ("CHECK_IN", "CHECK_OUT")
        if passed:
            self.open_barrier()
        # NFR đề 7: tổng thời gian nhận diện + mở barrier <= 1.5 s (chỉ đo lượt tự động, không có người can thiệp)
        m, self._m, perf = self._m, None, ""
        if passed and m and m["auto"]:
            ms = m["base"] + (time.perf_counter() - m["t0"]) * 1000
            self.svc.record_perf("GATE", ms)
            perf = f" | Thời gian xử lý: {ms / 1000:.2f} s"
            if ms / 1000 > config.PERF_TARGET_S:
                perf += f" (VƯỢT {config.PERF_TARGET_S} s)"
                kind = "WARN"
        self.set_status(o.message + perf, kind)
        if o.plate:
            self.set_plate(o.plate, f"Phí: {o.fee:,} đ" if o.action == "CHECK_OUT" else "")
        if not passed or kind == "WARN":
            self.beep()
        self.refresh_matrix()
        if passed and expired:                                # hết hạn: hiện hộp thoại để bảo vệ chú ý
            what = "thu phí vé lẻ" + (f" {o.fee:,} đ" if o.action == "CHECK_OUT" else " khi xe ra")
            self.after(50, lambda: messagebox.showwarning(
                "VÉ THÁNG HẾT HẠN", f"Biển {o.plate}\n{sub.label}\n\n=> Cần {what}.\nBáo chủ xe gia hạn vé tháng.",
                parent=self))

    def yellow_dialog(self, o, key, img, method):
        self._m = None                                        # bảo vệ phải quyết định -> không đo 1.5 s
        win = tk.Toplevel(self)
        win.title("CẢNH BÁO VÀNG - biển gần giống")
        win.configure(bg="#ffe680")
        win.transient(self.winfo_toplevel())
        win.grab_set()
        n = o.near_log
        tk.Label(win, bg="#ffe680", justify="left", font=("Segoe UI", 12), padx=16, pady=12,
                 text=f"Biển vừa đọc:  {o.plate}\nBiển trong bãi:  {n['plate_text']}  (ô {n['slot_code']}, vào {n['time_in']})\n"
                      f"Khoảng cách Levenshtein = {o.distance}\n\nBảo vệ hãy đối chiếu và quyết định:").pack()
        btns = tk.Frame(win, bg="#ffe680")
        btns.pack(pady=10)

        def same():
            win.destroy()
            self.show_outcome(self.svc.confirm_near_checkout(n["id"], img, "MANUAL", self.user["id"], o.plate))

        def other():
            win.destroy()
            self.svc.reject_near(o.plate, n, self.user["id"])
            self.show_outcome(self.svc.check_in(o.plate, key, img, "MANUAL", self.user["id"]))

        ttk.Button(btns, text="Cùng xe -> cho ra", command=same).pack(side="left", padx=6)
        ttk.Button(btns, text="Xe khác -> cho vào", command=other).pack(side="left", padx=6)
        ttk.Button(btns, text="Hủy", command=win.destroy).pack(side="left", padx=6)
        self.set_status(o.message + " - chờ bảo vệ quyết định", "WARN")

    def do_find(self, text, key):
        m = self.svc.find_car(key)
        if m.level == "EXACT":
            self.highlight = m.log["slot_code"]
            self.set_status(f"Xe {m.log['plate_text']} đang đỗ ở ô {m.log['slot_code']} (vào {m.log['time_in']})",
                            "CHECK_OUT")
            self.after(6000, self._clear_highlight)
        elif m.level == "NEAR":
            self.highlight = m.log["slot_code"]
            self.set_status(f"Không có đúng {text}. Có thể là {m.log['plate_text']} ở ô {m.log['slot_code']}", "WARN")
            self.after(6000, self._clear_highlight)
        else:
            self.set_status(f"Xe {text} không có trong bãi", "ERR")
        self.refresh_matrix()

    def _clear_highlight(self):
        self.highlight = None
        self.refresh_matrix()

    def do_lost(self, text, key, frame):
        """UC4 (mất thẻ / không khớp biển nào): Top N xe giống nhất HOẶC tất cả xe trong bãi,
        kèm ẢNH LÚC VÀO để bảo vệ đối chiếu với xe đang đứng ở cổng."""
        self._m = None
        if not self.svc.db.open_logs():
            self.set_status("Bãi đang trống, không có xe để đối chiếu", "ERR")
            return
        win = tk.Toplevel(self)
        win.title(f"Mất thẻ - đối chiếu xe với biển '{text}'")
        win.transient(self.winfo_toplevel())
        win.grab_set()
        show_all, ids, photos = tk.BooleanVar(value=False), {}, {}
        head = ttk.Frame(win)
        head.pack(fill="x", padx=8, pady=6)
        ttk.Label(head, text=f"Biển đọc tại cổng: {text}", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Checkbutton(head, text="Hiện TẤT CẢ xe trong bãi (tìm theo ảnh / giờ vào)", variable=show_all,
                        command=lambda: load()).pack(side="right")
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=8)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        tree = make_tree(left, ("Biển số", "Ô", "Giờ vào", "Khác biệt"), (120, 50, 150, 70), height=10)
        right = ttk.Frame(body)
        right.pack(side="right", padx=8)
        img_in = ttk.Label(right, text="(chọn một xe)", anchor="center", relief="groove", width=42)
        img_in.pack(pady=(6, 2), ipady=40)
        cap_in = ttk.Label(right, text="Ảnh lúc VÀO của xe đang chọn")
        cap_in.pack()
        img_out = ttk.Label(right, text="(không có ảnh)", anchor="center", relief="groove", width=42)
        img_out.pack(pady=(10, 2), ipady=40)
        ttk.Label(right, text="Ảnh xe đang ở cổng (lúc RA)").pack()
        photos["out"] = thumb(frame) if frame is not None else None
        if photos["out"]:
            img_out.configure(image=photos["out"], text="")

        def load():
            tree.delete(*tree.get_children())
            ids.clear()
            for d, l in self.svc.lost_candidates(key, show_all.get()):
                ids[tree.insert("", "end", values=(l["plate_text"], l["slot_code"], l["time_in"], d))] = l
            first = tree.get_children()
            if first:
                tree.selection_set(first[0])

        def on_select(_=None):
            sel = tree.selection()
            if not sel:
                return
            l = ids[sel[0]]
            photos["in"] = thumb(l["image_in"]) if l.get("image_in") else None
            if photos["in"]:
                img_in.configure(image=photos["in"], text="")
            else:
                img_in.configure(image="", text="(không có ảnh lúc vào)")
            cap_in.configure(text=f"Ảnh lúc VÀO - {l['plate_text']} - {l['time_in']}")

        def choose():
            sel = tree.selection()
            if not sel:
                return messagebox.showinfo("Chọn xe", "Hãy chọn một xe trong danh sách", parent=win)
            l = ids[sel[0]]
            fee = self.svc.db.active_pricing()["lost_ticket_fee"]
            if messagebox.askyesno("Xác nhận", f"Cho xe {l['plate_text']} (ô {l['slot_code']}) ra và thu phí mất thẻ "
                                   f"{fee:,} đ?", parent=win):
                img = save_image(frame, "lost", key) if frame is not None else None
                o = self.svc.checkout_lost(l["id"], img, self.user["id"], text)
                win.destroy()
                self.show_outcome(o)

        tree.bind("<<TreeviewSelect>>", on_select)
        ttk.Button(win, text="Chọn xe này & cho ra (phí mất thẻ)", command=choose).pack(pady=8)
        load()

    # ---------- barrier ----------
    def open_barrier(self):
        self.barrier.configure(text="BARRIER: ĐANG MỞ", bg="#2e7d32")
        self.after(3000, lambda: self.barrier.configure(text="BARRIER: ĐÓNG", bg="#c62828"))

    # ---------- ma trận 3x3 ----------
    def refresh_matrix(self):
        occ = {l["slot_code"]: l for l in self.svc.db.open_logs()}
        for code, lbl in self.cells.items():
            log = occ.get(code)
            color, extra = COLORS["free"], ""
            if log:
                color, info = COLORS["occ"], self.svc.subscription_status(log["plate_key"])
                if info.status == "ACTIVE":
                    color, extra = (COLORS["soon"], "VÉ THÁNG (sắp hết)") if info.soon else (COLORS["sub"], "VÉ THÁNG")
                elif info.status in ("EXPIRED", "NOT_STARTED"):
                    color, extra = COLORS["exp"], "VÉ THÁNG HẾT HẠN" if info.status == "EXPIRED" else "VÉ THÁNG CHƯA HL"
            if code == self.highlight:
                color = COLORS["find"]
            if code == self.move_src:
                color = COLORS["sel"]
            lbl.configure(text=f"{code}\n{log['plate_text'] if log else 'trống'}" + (f"\n{extra}" if extra else ""),
                          bg=color)
        try:                                                    # thẻ số liệu phía trên
            today = date.today().isoformat()
            day = next(iter(self.svc.db.daily_stats(today, today)), None)
            self.kpi["in"].configure(text=f"{len(occ)}/{len(self.cells)}")
            self.kpi["free"].configure(text=str(len(self.cells) - len(occ)))
            self.kpi["vao"].configure(text=str(day["vao"] if day else 0))
            self.kpi["rev"].configure(text=f"{(day['doanh_thu'] if day else 0):,} đ")
        except Exception:
            pass

    def _reset_move(self):
        self.move_src = None
        self.refresh_matrix()

    def on_cell(self, code):
        if not self.move_mode.get():
            return
        occupied = {l["slot_code"] for l in self.svc.db.open_logs()}
        if self.move_src is None:
            if code in occupied:
                self.move_src = code
            else:
                self.set_status(f"Ô {code} đang trống, hãy chọn ô có xe", "ERR")
        else:
            try:
                self.svc.move_car(self.move_src, code, self.user["id"])
                self.set_status(f"Đã di dời xe từ {self.move_src} sang {code}", "CHECK_OUT")
            except ValueError as e:
                messagebox.showerror("Không thể di dời", str(e))
            self.move_src = None
        self.refresh_matrix()

    # ---------- sửa lỗi: đọc nhầm biển ----------
    def fix_dialog(self, select_code=None):
        """Danh sách xe đang trong bãi: sửa biển đọc sai, hoặc hủy lượt vào nhập nhầm."""
        win = tk.Toplevel(self)
        win.title("Sửa biển / Hủy lượt vào")
        win.transient(self.winfo_toplevel())
        win.grab_set()
        tk.Label(win, justify="left", padx=10, pady=6, text=(
            "Chọn xe rồi bấm:\n"
            "  - SỬA BIỂN: máy đọc sai ký tự, xe vẫn ở trong bãi.\n"
            f"  - HỦY LƯỢT VÀO: xe vào nhầm / quét nhầm (bảo vệ hủy được trong {config.VOID_WINDOW_MIN} phút đầu, "
            "Admin hủy bất cứ lúc nào). Không thu phí.")).pack(anchor="w")
        tree = make_tree(win, ("Ô", "Biển số", "Giờ vào", "Cách thức"), (60, 130, 160, 90), height=9)
        ids = {}

        def load():
            tree.delete(*tree.get_children())
            ids.clear()
            for l in self.svc.db.open_logs():
                iid = tree.insert("", "end", values=(l["slot_code"], l["plate_text"], l["time_in"], l["in_method"]))
                ids[iid] = l
                if select_code and l["slot_code"] == select_code:
                    tree.selection_set(iid)

        def chosen():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Chọn xe", "Hãy chọn một xe trong danh sách", parent=win)
                return None
            return ids[sel[0]]

        def fix():
            log = chosen()
            if not log:
                return
            text, key = self._ask_plate(f"Biển đang lưu: {log['plate_text']}\nNhập biển ĐÚNG:", log["plate_text"])
            if not key:
                return
            try:
                self.svc.correct_plate(log["id"], text, key, self.user["id"])
            except ValueError as e:
                return messagebox.showerror("Không sửa được", str(e), parent=win)
            self.set_status(f"Đã sửa biển {log['plate_text']} -> {text}", "CHECK_OUT")
            self.refresh_matrix()
            load()

        def void():
            log = chosen()
            if not log:
                return
            if not messagebox.askyesno("Hủy lượt vào", f"Hủy lượt vào của {log['plate_text']} (ô {log['slot_code']})?\n"
                                       "Xe bị xóa khỏi bãi, KHÔNG thu phí.", parent=win):
                return
            try:
                self.svc.void_checkin(log["id"], self.user["id"], self.user["role"])
            except (ValueError, PermissionError) as e:
                return messagebox.showerror("Không hủy được", str(e), parent=win)
            self.set_status(f"Đã hủy lượt vào {log['plate_text']} (ô {log['slot_code']} trống lại)", "WARN")
            self.refresh_matrix()
            load()

        btns = ttk.Frame(win)
        btns.pack(pady=8)
        ttk.Button(btns, text="Sửa biển", command=fix).pack(side="left", padx=6)
        ttk.Button(btns, text="Hủy lượt vào (nhập nhầm)", command=void).pack(side="left", padx=6)
        ttk.Button(btns, text="Đóng", command=win.destroy).pack(side="left", padx=6)
        load()

    def destroy(self):
        self.liveai.stop()
        self.cap.release()
        super().destroy()


# =====================================================================
#                              TAB ADMIN
# =====================================================================
class SubscriptionTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        theme.page_header(self, "Quản lý vé tháng", "thêm, gia hạn, xóa vé; đỏ = hết hạn, vàng = sắp hết")
        form = ttk.Frame(self)
        form.pack(fill="x", padx=6, pady=6)
        self.vars = {k: tk.StringVar() for k in ("plate", "owner", "start", "end")}
        self.vars["start"].set(date.today().isoformat())
        self.vars["end"].set((date.today() + timedelta(days=30)).isoformat())
        for label, k in (("Biển số", "plate"), ("Chủ xe", "owner"), ("Từ ngày", "start"), ("Đến ngày", "end")):
            ttk.Label(form, text=label).pack(side="left", padx=(8, 2))
            ttk.Entry(form, textvariable=self.vars[k], width=14).pack(side="left")
        ttk.Button(form, text="Thêm vé", command=self.add).pack(side="left", padx=8)
        ttk.Button(form, text="Xóa vé đã chọn", command=self.delete).pack(side="left", padx=(0, 8))
        ttk.Button(form, text="Gia hạn +30 ngày", command=self.renew).pack(side="left")
        self.tree = make_tree(self, ("ID", "Biển số", "Chủ xe", "Từ", "Đến", "Trạng thái"), (50, 130, 160, 110, 110, 100))
        self.tree.tag_configure("exp", background="#ffcdd2")
        self.tree.tag_configure("soon", background="#ffe9b0")
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        today = date.today()
        for s in self.app.db.list_subscriptions():
            end = date.fromisoformat(s["end_date"])
            if s["start_date"] > today.isoformat():
                st, tag = "Chưa bắt đầu", "soon"
            elif end < today:
                st, tag = f"HẾT HẠN ({(today - end).days} ngày)", "exp"
            else:
                left = (end - today).days
                st, tag = (f"Còn {left} ngày" + (" - sắp hết" if left <= config.SUB_WARN_DAYS else "")), \
                          ("soon" if left <= config.SUB_WARN_DAYS else "")
            self.tree.insert("", "end", tags=(tag,),
                             values=(s["id"], s["plate_text"], s["owner_name"], s["start_date"], s["end_date"], st))

    def renew(self):
        """Gia hạn thêm 30 ngày (có ghi Audit Log)."""
        sel = self.tree.selection()
        if not sel:
            return messagebox.showinfo("Gia hạn", "Hãy chọn một vé trong danh sách")
        key = normalize(str(self.tree.item(sel[0])["values"][1]))
        if not messagebox.askyesno("Gia hạn", f"Gia hạn thêm 30 ngày cho {pretty(key)}?"):
            return
        try:
            start, end = self.app.svc.renew_subscription(key, self.app.user["id"])
        except ValueError as e:
            return messagebox.showerror("Lỗi", str(e))
        self.refresh()
        messagebox.showinfo("Gia hạn", f"Đã gia hạn: {start} -> {end}")

    def add(self):
        v = {k: x.get().strip() for k, x in self.vars.items()}
        key = normalize(v["plate"])
        if not is_valid(key):
            return messagebox.showerror("Lỗi", "Biển số không hợp lệ")
        try:
            date.fromisoformat(v["start"]); date.fromisoformat(v["end"])
            if v["end"] < v["start"]:
                raise ValueError("Ngày kết thúc phải sau ngày bắt đầu")
            self.app.svc.add_subscription(key, pretty(key), v["owner"], v["start"], v["end"], self.app.user["id"])
        except ValueError as e:
            return messagebox.showerror("Lỗi", str(e))
        self.refresh()

    def delete(self):
        sel = self.tree.selection()
        if sel and messagebox.askyesno("Xác nhận", "Xóa vé tháng đã chọn?"):
            self.app.svc.delete_subscription(self.tree.item(sel[0])["values"][0], self.app.user["id"])
            self.refresh()


class AuditTab(ttk.Frame):
    """Nhật ký kiểm toán (Audit Log): ai làm gì, lúc nào, sửa từ gì thành gì. Chỉ đọc - không sửa/xóa được."""
    PAGE = 50

    def __init__(self, master, app):
        super().__init__(master)
        self.app, self.cursors, self.pos = app, [None], 0
        theme.page_header(self, "Audit Log", "nhật ký kiểm toán - chỉ đọc, không sửa/xóa được")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bar, text="< Mới hơn", command=self.prev).pack(side="left")
        ttk.Button(bar, text="Cũ hơn >", command=self.next).pack(side="left", padx=4)
        self.mode = tk.StringVar(value="Chỉ thao tác sửa tay / nhạy cảm")
        cb = ttk.Combobox(bar, textvariable=self.mode, state="readonly", width=32,
                          values=["Chỉ thao tác sửa tay / nhạy cảm", "Tất cả sự kiện"])
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.reload())
        ttk.Button(bar, text="Làm mới", command=self.reload).pack(side="left")
        ttk.Button(bar, text="Xuất Excel", command=self.export).pack(side="left", padx=8)
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=8)
        self.tree = make_tree(self, ("ID", "Thời gian", "Người thao tác", "Hành động", "Biển số", "Chi tiết"),
                              (50, 140, 110, 210, 100, 480), height=18)
        self.tree.column("Chi tiết", anchor="w")
        self.load()

    def _types(self):
        return AUDIT_SENSITIVE if self.mode.get().startswith("Chỉ") else None

    def load(self):
        rows = self.app.db.audit_page(self.cursors[self.pos], self.PAGE, self._types())
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            who = f"{r['username']} ({r['role']})" if r["username"] else "(hệ thống)"
            self.tree.insert("", "end", values=(r["id"], r["event_time"], who,
                                                EVENT_LABELS.get(r["event_type"], r["event_type"]),
                                                r["plate_text"] or "", r["detail"] or ""))
        self.has_next = len(rows) == self.PAGE
        self.next_cursor = rows[-1]["id"] if rows else None
        self.info.configure(text=f"Trang {self.pos + 1} ({len(rows)} dòng)")

    def next(self):
        if self.has_next:
            if self.pos + 1 >= len(self.cursors):
                self.cursors.append(self.next_cursor)
            self.pos += 1
            self.load()

    def prev(self):
        if self.pos > 0:
            self.pos -= 1
            self.load()

    def reload(self):
        self.cursors, self.pos = [None], 0
        self.load()

    def export(self):
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialdir=str(config.EXPORT_DIR),
                                            initialfile="audit_log.xlsx")
        if path:
            self.app.svc.export_audit(path, sensitive_only=self._types() is not None)
            messagebox.showinfo("Xuất Excel", f"Đã lưu:\n{path}")


class HistoryTab(ttk.Frame):
    PAGE = 50

    def __init__(self, master, app):
        super().__init__(master)
        self.app, self.cursors, self.pos = app, [None], 0
        theme.page_header(self, "Lịch sử ra/vào", "tất cả lượt xe, mới nhất trước")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bar, text="< Mới hơn", command=self.prev).pack(side="left")
        ttk.Button(bar, text="Cũ hơn >", command=self.next).pack(side="left", padx=4)
        ttk.Button(bar, text="Làm mới", command=self.reload).pack(side="left")
        self.q = tk.StringVar()
        ttk.Label(bar, text="Tìm biển:").pack(side="left", padx=(14, 2))
        e = ttk.Entry(bar, textvariable=self.q, width=14)
        e.pack(side="left")
        e.bind("<Return>", lambda _e: self.reload())
        ttk.Button(bar, text="Tìm", command=self.reload).pack(side="left", padx=4)
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=10)
        self.tree = make_tree(self, ("ID", "Biển số", "Giờ vào", "Giờ ra", "Ô", "Phí", "Loại phí", "Trạng thái"),
                              (50, 110, 150, 150, 50, 80, 110, 80), height=18)
        self.load()

    def load(self):
        rows = self.app.db.history_page(self.cursors[self.pos], self.PAGE, self.q.get().strip() or None)
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", values=(r["id"], r["plate_text"], r["time_in"], r["time_out"] or "", r["slot_code"] or "",
                                                f"{r['fee']:,}", r["fee_type"] or "", r["status"]))
        self.has_next = len(rows) == self.PAGE
        self.next_cursor = rows[-1]["id"] if rows else None
        self.info.configure(text=f"Trang {self.pos + 1} ({len(rows)} dòng)")

    def next(self):
        if self.has_next:
            if self.pos + 1 >= len(self.cursors):
                self.cursors.append(self.next_cursor)
            self.pos += 1
            self.load()

    def prev(self):
        if self.pos > 0:
            self.pos -= 1
            self.load()

    def reload(self):
        self.cursors, self.pos = [None], 0
        self.load()


class StatsTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        theme.page_header(self, "Thống kê & Báo cáo", "xe vào/ra, doanh thu theo ngày, hiệu năng cổng")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=6)
        today = date.today()
        self.d_from = tk.StringVar(value=today.replace(day=1).isoformat())
        self.d_to = tk.StringVar(value=today.isoformat())
        ttk.Label(bar, text="Từ").pack(side="left")
        ttk.Entry(bar, textvariable=self.d_from, width=12).pack(side="left", padx=4)
        ttk.Label(bar, text="Đến").pack(side="left")
        ttk.Entry(bar, textvariable=self.d_to, width=12).pack(side="left", padx=4)
        ttk.Button(bar, text="Xem thống kê", command=self.refresh).pack(side="left", padx=6)
        ttk.Button(bar, text="Xuất Excel", command=self.export).pack(side="left")
        self.summary = ttk.Label(bar, text="", font=("Segoe UI", 10, "bold"))
        self.summary.pack(side="left", padx=14)
        self.perf = ttk.Label(self, text="", foreground="#1565c0")
        self.perf.pack(fill="x", padx=10)
        self.tree = make_tree(self, ("Ngày", "Xe vào", "Xe ra", "Doanh thu (đ)"), (140, 100, 100, 140), height=18)
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        rows = self.app.db.daily_stats(self.d_from.get(), self.d_to.get())
        for r in rows:
            self.tree.insert("", "end", values=(r["day"], r["vao"], r["ra"], f"{r['doanh_thu']:,}"))
        self.summary.configure(text=f"Đang trong bãi: {self.app.db.total_in_lot()} xe | "
                                    f"Tổng doanh thu: {sum(r['doanh_thu'] for r in rows):,} đ")
        p = self.app.db.perf_summary("GATE")
        self.perf.configure(text=("Hiệu năng cổng (nhận diện -> mở barrier, lượt tự động): "
                                  f"{p['n']} lượt | trung vị {p['p50'] / 1000:.2f} s | P95 {p['p95'] / 1000:.2f} s | "
                                  f"đạt <= {config.PERF_TARGET_S} s: {p['ok_pct']:.0f}%") if p else
                            f"Hiệu năng cổng: chưa có số liệu (cần >= 1 lượt quét tự động; chỉ tiêu <= {config.PERF_TARGET_S} s)")

    def export(self):
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialdir=str(config.EXPORT_DIR),
                                            initialfile=f"doanh_thu_{self.d_from.get()}_{self.d_to.get()}.xlsx")
        if path:
            self.app.svc.export_excel(self.d_from.get(), self.d_to.get(), path)
            messagebox.showinfo("Xuất Excel", f"Đã lưu:\n{path}")


# =====================================================================
#                           ỨNG DỤNG CHÍNH
# =====================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bãi xe thông minh - Hệ thống quản lý bãi giữ xe ANPR")
        self.geometry("1320x820")
        self.minsize(1180, 680)
        theme.apply(self)                                    # giao diện hiện đại (lỗi thì tự dùng giao diện mặc định)
        self.db = Database()
        self.svc = ParkingService(self.db)
        self.user, self.pipeline = None, None
        self.web_url = ""
        if config.API_ENABLED:                                # REST API + Swagger UI cho máy thứ hai trong cùng mạng LAN
            try:
                self.api, self.web_url = api_server.start(pipeline_getter=lambda: self.pipeline)
            except Exception as e:
                print(f"Không bật được REST API (cổng {config.API_PORT} đang bận?): {e}")
        if config.ADMIN_WEB_ENABLED:                          # web Admin HTML cũ (mặc định tắt)
            try:
                self.web, legacy_url = admin_web.start()
                self.web_url = self.web_url or legacy_url
            except OSError as e:
                print(f"Không bật được web Admin (cổng {config.ADMIN_WEB_PORT} đang bận?): {e}")
        threading.Thread(target=self._load_pipeline, daemon=True).start()   # tải YOLO/OCR ở nền
        self.login_ui()

    def _load_pipeline(self):
        self.pipeline = AnprPipeline()

    def login_ui(self):
        for w in self.winfo_children():
            w.destroy()
        self.configure(bg=theme.NAVY)
        wrap = tk.Frame(self, bg=theme.NAVY)
        wrap.place(relx=0.5, rely=0.46, anchor="center")
        tk.Label(wrap, text="BÃI XE THÔNG MINH", bg=theme.NAVY, fg="white", font=theme.font(28, True)).pack()
        tk.Label(wrap, text="Hệ thống quản lý bãi giữ xe  ·  Nhận diện biển số tự động (ANPR)", bg=theme.NAVY,
                 fg="#9fb3c8", font=theme.font(11)).pack(pady=(2, 20))
        box = tk.Frame(wrap, bg=theme.WHITE, padx=34, pady=28)
        box.pack()
        tk.Label(box, text="Đăng nhập", bg=theme.WHITE, fg=theme.TEXT, font=theme.font(16, True)).pack(anchor="w",
                                                                                                    pady=(0, 14))
        u, p = tk.StringVar(), tk.StringVar()
        tk.Label(box, text="TÀI KHOẢN", bg=theme.WHITE, fg=theme.MUTED, font=theme.font(9, True)).pack(anchor="w")
        e1 = ttk.Entry(box, textvariable=u, width=34, font=theme.font(11))
        e1.pack(pady=(2, 10))
        tk.Label(box, text="MẬT KHẨU", bg=theme.WHITE, fg=theme.MUTED, font=theme.font(9, True)).pack(anchor="w")
        e2 = ttk.Entry(box, textvariable=p, show="*", width=34, font=theme.font(11))
        e2.pack(pady=(2, 0))

        def go(_=None):
            user = self.svc.login(u.get().strip(), p.get())
            if not user:
                return messagebox.showerror("Đăng nhập", "Sai tài khoản hoặc mật khẩu")
            self.user = user
            self.svc.log_event("LOGIN", None, user["username"], user["id"])
            self.main_ui()

        ttk.Button(box, text="ĐĂNG NHẬP", style="Accent.TButton", command=go).pack(fill="x", pady=(20, 0))
        e1.bind("<Return>", go)
        e2.bind("<Return>", go)
        e1.focus()

    def main_ui(self):
        for w in self.winfo_children():
            w.destroy()
        self.configure(bg=theme.BG)
        top = tk.Frame(self, bg=theme.NAVY)                  # thanh tiêu đề
        top.pack(fill="x")
        tk.Label(top, text="BÃI XE THÔNG MINH", bg=theme.NAVY, fg="white", font=theme.font(15, True),
                 padx=16, pady=11).pack(side="left")
        tk.Label(top, text="ANPR  ·  nhận diện biển số tự động", bg=theme.NAVY, fg="#9fb3c8",
                 font=theme.font(10)).pack(side="left")
        ttk.Button(top, text="Đăng xuất", style="Header.TButton", command=self.logout).pack(side="right", padx=14)
        role = "Quản trị viên" if self.user["role"] == "ADMIN" else "Bảo vệ"
        tk.Label(top, text=f"{self.user['full_name']}  ·  {role}", bg=theme.NAVY2, fg="white", font=theme.font(10, True),
                 padx=12, pady=5).pack(side="right", padx=(0, 10))
        self.clock_lbl = tk.Label(top, text="", bg=theme.NAVY, fg="#cfe3ff", font=theme.font(11, True), padx=14)
        self.clock_lbl.pack(side="right")
        self._tick_clock()
        if self.web_url:
            tk.Label(top, text=f"Swagger UI / API (cùng mạng):  {self.web_url}", bg=theme.NAVY, fg="#7fc4ff",
                     font=theme.font(9), padx=16).pack(side="right")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.guard = GuardTab(nb, self)
        nb.add(self.guard, text="Cổng (Bảo vệ)")
        if self.user["role"] == "ADMIN":                      # phân quyền: chỉ Admin thấy các tab này
            nb.add(SubscriptionTab(nb, self), text="Vé tháng")
            nb.add(HistoryTab(nb, self), text="Lịch sử ra/vào")
            nb.add(AuditTab(nb, self), text="Audit Log")
            nb.add(StatsTab(nb, self), text="Thống kê & Báo cáo")

    def _tick_clock(self):
        try:
            if self.clock_lbl.winfo_exists():
                self.clock_lbl.configure(text=time.strftime("%H:%M:%S   %d/%m/%Y"))
                self.after(1000, self._tick_clock)
        except tk.TclError:
            pass

    def logout(self):
        self.user = None
        self.login_ui()


if __name__ == "__main__":
    App().mainloop()
