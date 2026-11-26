"""Giao diện hiện đại (kiểu phần mềm bãi xe / bảng điều khiển thực tế) chỉ dùng tkinter + ttk chuẩn - không cần cài thêm.

Bảng màu: thanh tiêu đề xanh navy, nền xám nhạt, "thẻ" trắng có viền mảnh, nút chính xanh dương,
banner kết quả to rõ (xanh = mời vào, xanh dương = mời ra, vàng = chú ý, đỏ = từ chối), biển số hiển thị như biển thật.
Nếu theme lỗi vì lý do nào đó (apply() bắt mọi ngoại lệ) thì app vẫn chạy với giao diện mặc định.
"""
import tkinter as tk
from tkinter import ttk

NAVY, NAVY2 = "#0f2a47", "#1b3d66"
ACCENT, ACCENT_DK = "#1e88e5", "#1565c0"
GREEN, GREEN_DK = "#2e7d32", "#1b5e20"
AMBER, RED, RED_DK = "#e0a100", "#c62828", "#8e0000"
BG, WHITE, LINE = "#eef1f5", "#ffffff", "#dde3ea"
TEXT, MUTED, SLATE = "#1f2937", "#6b7280", "#546e7a"
FAMILY = "Segoe UI"

# Banner kết quả: (chữ to, màu nền)
BANNER = {"CHECK_IN": ("MỜI VÀO", GREEN), "CHECK_OUT": ("MỜI RA", ACCENT_DK), "WARN": ("CHÚ Ý", AMBER),
          "ERR": ("TỪ CHỐI / LỖI", RED), "INFO": ("THÔNG TIN", SLATE)}


def font(size=10, bold=False):
    return (FAMILY, size, "bold") if bold else (FAMILY, size)


PLATE_FONT = ("Consolas", 26, "bold")


def apply(root):
    """Áp dụng style cho toàn bộ ttk. Trả về True nếu thành công."""
    try:
        root.configure(bg=BG)
        st = ttk.Style(root)
        st.theme_use("clam")                                  # 'clam' cho phép tô màu nút/tab/bảng đầy đủ
        st.configure(".", font=font(10), background=BG, foreground=TEXT)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("TCheckbutton", background=BG, foreground=TEXT)
        st.configure("Card.TCheckbutton", background=WHITE, foreground=TEXT)
        st.configure("TEntry", padding=6, fieldbackground=WHITE)
        st.configure("TCombobox", padding=5, fieldbackground=WHITE)
        st.configure("TLabelframe", background=BG)
        # --- nút ---
        st.configure("TButton", padding=(12, 7), background="#e3e8ee", foreground=TEXT, borderwidth=0, relief="flat")
        st.map("TButton", background=[("pressed", "#c9d2dd"), ("active", "#d5dce6"), ("disabled", "#eef1f5")])
        for name, base, dark in (("Accent", ACCENT, ACCENT_DK), ("Success", GREEN, GREEN_DK), ("Danger", RED, RED_DK),
                                 ("Header", NAVY2, "#27508a")):
            st.configure(f"{name}.TButton", background=base, foreground="white", font=font(11, True),
                         padding=(16, 10), borderwidth=0)
            st.map(f"{name}.TButton", background=[("pressed", dark), ("active", dark), ("disabled", "#9aa7b4")],
                   foreground=[("disabled", "#e6ebf0")])
        st.configure("Header.TButton", font=font(10, True), padding=(12, 6))
        # --- bảng dữ liệu ---
        st.configure("Treeview", rowheight=28, background=WHITE, fieldbackground=WHITE, foreground=TEXT,
                     borderwidth=0, font=font(10))
        st.configure("Treeview.Heading", background=NAVY, foreground="white", font=font(10, True), padding=7,
                     relief="flat")
        st.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "white")])
        st.map("Treeview.Heading", background=[("active", NAVY2)])
        # --- tab ---
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(12, 8, 12, 0))
        st.configure("TNotebook.Tab", padding=(20, 9), font=font(10, True), background="#dbe2ea", foreground=MUTED,
                     borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", WHITE)], foreground=[("selected", ACCENT_DK)])
        st.configure("Vertical.TScrollbar", background="#cfd6df", troughcolor=BG, borderwidth=0)
        return True
    except Exception as e:                                     # giao diện mặc định vẫn dùng được
        print("Không áp dụng được theme, dùng giao diện mặc định:", e)
        return False


def card(parent, title=None):
    """Thẻ trắng có viền mảnh. Trả về (khung ngoài để pack, khung trong để đặt nội dung)."""
    outer = tk.Frame(parent, bg=WHITE, highlightthickness=1, highlightbackground=LINE)
    if title:
        tk.Label(outer, text=title, bg=WHITE, fg=MUTED, font=font(9, True), anchor="w", padx=14, pady=9).pack(fill="x")
        tk.Frame(outer, bg=LINE, height=1).pack(fill="x")
    body = tk.Frame(outer, bg=WHITE)
    body.pack(fill="both", expand=True, padx=14, pady=10)
    return outer, body


def stat_card(parent, title, color):
    """Thẻ số liệu: vạch màu bên trái, tiêu đề nhỏ, số to. Trả về (khung, nhãn số, nhãn phụ)."""
    outer = tk.Frame(parent, bg=WHITE, highlightthickness=1, highlightbackground=LINE)
    tk.Frame(outer, bg=color, width=6).pack(side="left", fill="y")
    inner = tk.Frame(outer, bg=WHITE)
    inner.pack(side="left", fill="both", expand=True, padx=14, pady=6)
    tk.Label(inner, text=title, bg=WHITE, fg=MUTED, font=font(8, True), anchor="w").pack(fill="x")
    value = tk.Label(inner, text="-", bg=WHITE, fg=TEXT, font=font(18, True), anchor="w")
    value.pack(fill="x")
    sub = tk.Label(inner, text="", bg=WHITE, fg=MUTED, font=font(9), anchor="w")   # (không hiển thị, dành cho mở rộng)
    return outer, value, sub


def page_header(parent, title, subtitle=""):
    """Tiêu đề trang cho các tab quản trị."""
    f = tk.Frame(parent, bg=BG)
    f.pack(fill="x", padx=14, pady=(12, 2))
    tk.Label(f, text=title, bg=BG, fg=NAVY, font=font(16, True), anchor="w").pack(side="left")
    if subtitle:
        tk.Label(f, text="   " + subtitle, bg=BG, fg=MUTED, font=font(10), anchor="w").pack(side="left")
    return f


def plate_box(parent):
    """Ô hiển thị biển số giống biển thật (nền trắng, viền đen đậm). Trả về (khung, nhãn chữ)."""
    box = tk.Frame(parent, bg="#fdfdfd", highlightthickness=4, highlightbackground="#111111")
    lbl = tk.Label(box, text="- - -", bg="#fdfdfd", fg="#111111", font=PLATE_FONT, padx=16, pady=4)
    lbl.pack()
    return box, lbl
