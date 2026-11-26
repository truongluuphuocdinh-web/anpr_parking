# Hệ thống quản lý bãi giữ xe ANPR (đồ án DAMH)

Python + Tkinter + OpenCV + YOLO + EasyOCR + SQLite + FastAPI (REST API, Swagger UI). Bãi giả lập 3x3 (A1..C3).

## Cài đặt
```
python -m venv venv && venv\Scripts\activate      # Windows (Linux/Mac: source venv/bin/activate)
pip install -r requirements.txt
python app.py
```
Tài khoản mặc định: `admin / admin123` (Admin), `baove / baove123` (Bảo vệ). Đổi trong `config.py` trước khi nộp.
OCR chính là RapidOCR (mô hình có sẵn trong gói pip, không phải tải thêm). EasyOCR chỉ là dự phòng. Nếu OCR chưa sẵn sàng, giao diện báo và vẫn nhập biển tay được. Đổi lựa chọn ở `OCR_BACKEND` trong `config.py`.

## Cấu trúc
| File | Vai trò (khớp sơ đồ lớp) |
|---|---|
| `app.py` | Giao diện: GuardScreen, AdminScreen |
| `services.py` | ParkingService, FeeService, tìm xe, di dời, báo cáo/Excel |
| `anpr.py` | PlateDetector (YOLO), PlateOCR (RapidOCR/EasyOCR), AnprPipeline |
| `debug_ocr.py` | Chẩn đoán nhận diện: `python debug_ocr.py anh.png` (lưu ảnh vào `debug/`) |
| `plate_utils.py` | Chuẩn hóa biển, kiểm tra định dạng, Levenshtein |
| `api_server.py` | **REST API + Swagger UI** (FastAPI): `/docs`, `/openapi.json` |
| `export_openapi.py` | Xuất mô tả API ra file `openapi.json` |
| `static/swagger/` | Swagger UI chép sẵn (chạy được khi mạng LAN không có internet) |
| `db.py` | Schema SQLite + các Repository (gộp một lớp) |
| `config.py` | Ngưỡng tin cậy, bảng giá, tài khoản mặc định |
| `make_demo_plates.py` | Tạo ảnh biển mẫu cho laptop "khách hàng" |
| `tests/` | Test nghiệp vụ, phí, pipeline, OCR — chạy `python -m pytest -q` |

## Quy trình demo
1. `python make_demo_plates.py 59D1-123.45 51F-123.45 59O1-123.45` rồi mở ảnh trên laptop khách hàng.
2. Đăng nhập `baove`. Hướng camera vào ảnh, bấm **QUÉT BIỂN**:
   biển chưa có -> **check-in**, xe vào ô ngẫu nhiên; quét lại -> **check-out**, tính phí, mở barrier.
3. **TÌM XE**: quét biển -> ô đang đỗ sáng vàng. Bật "chế độ di dời" để chuyển xe giữa các ô.
4. Ngoại lệ: ảnh mờ/lóa -> còi + nhập tay. Biển lệch 1 ký tự (59D1 vs 59O1) -> **cảnh báo vàng**.
   **Mất thẻ / Top 5** -> chọn lượt In đúng, thu phí mất thẻ.
5. Đăng nhập `admin`: vé tháng, lịch sử (phân trang), thống kê, xuất Excel.
Không có camera? Bấm **Mở ảnh (thay camera)** để chọn ảnh từ máy.

## Giao diện (theme.py)
Giao diện kiểu phần mềm bãi xe thực tế, chỉ dùng tkinter/ttk chuẩn (không cần cài thêm):
- Thanh tiêu đề xanh navy: tên hệ thống, đồng hồ, người dùng + vai trò, địa chỉ Swagger UI, nút Đăng xuất. Màn hình đăng nhập dạng thẻ giữa màn hình.
- Màn hình Cổng: 4 thẻ số liệu (xe trong bãi, còn trống, xe vào hôm nay, doanh thu hôm nay); camera + nút QUÉT BIỂN to màu xanh dương; **banner kết quả** to (xanh MỜI VÀO / xanh dương MỜI RA / vàng CHÚ Ý / đỏ TỪ CHỐI) kèm **ô biển số giống biển thật** và số tiền phải thu; sơ đồ bãi tô màu theo loại xe; nhật ký hoạt động có giờ.
- Các tab quản trị có tiêu đề trang, bảng có tiêu đề navy, dòng cao 28px, tab chọn nổi bật.
- Cửa sổ cao khoảng 820px. Màn hình thấp (laptop 768px): đặt `UI_COMPACT = True` trong `config.py` để ẩn khung nhật ký.
- Nếu theme lỗi vì lý do nào đó, app tự dùng giao diện mặc định của Tkinter.
- Đổi màu/phông: sửa hằng số ở đầu `theme.py` (NAVY, ACCENT, GREEN, ...).
- `tests/fake_tk.py` là Tkinter giả (không cần màn hình) dùng để chạy thử các luồng giao diện, kiểm tra cả tên tùy chọn widget.

## Đối chiếu với Đề tài 7 (ALPR bãi giữ xe)
| Yêu cầu đề 7 | Thực hiện trong app | Cách kiểm chứng |
|---|---|---|
| UC1 Check-in: YOLO + OCR, lưu ảnh + giờ vào; lóa/bẩn -> còi, bảo vệ gõ tay | Tab Cổng; sai định dạng -> còi + hộp nhập tay | Mở ảnh mờ -> hộp nhập tay xuất hiện |
| UC2 Check-out: khớp 100% -> tính tiền, mở barrier; Levenshtein <= 1 -> cảnh báo vàng | `services.process_scan`, hộp "CẢNH BÁO VÀNG" | Vào `59D1-123.45`, ra `59O1-123.45` |
| UC3 Vé tháng: whitelist, không tính tiền; hết hạn -> báo + thu vé lẻ | Tab Vé tháng, nhãn trên video, màu ô bãi | Thêm vé hết hạn rồi quét |
| UC4 Mất thẻ: tìm biển gần giống **hoặc theo ảnh giờ vào**; Top 5 | Nút "Mất thẻ": Top 5 / tất cả xe + **ảnh lúc vào và ảnh xe ở cổng** | Quét biển lạ ở chế độ mất thẻ |
| UC5 Thống kê: xe trong bãi, vào/ra theo ngày, Excel doanh thu; phân trang + index | Tab Thống kê, Lịch sử (phân trang keyset + tìm biển), index trong `db.py` | Nút Xuất Excel |
| NFR hiệu năng: nhận diện + mở barrier <= 1.5 s | Đo từng lượt quét tự động -> `perf_log`; hiện trên thanh trạng thái, cảnh báo nếu vượt; tab Thống kê hiện trung vị / P95 / % đạt | Xem dòng "Hiệu năng cổng" |
| NFR độ chính xác ký tự > 95% (ban ngày) | `python evaluate.py thu_muc_anh` -> % đúng nguyên biển, % ký tự, thời gian, file CSV | Chạy trên ảnh thật |
| NFR bảo mật: mọi sửa tay In/Out có Audit Log | Bảng `event_log` **chỉ thêm, không sửa/xóa được** (trigger SQLite); tab Audit Log; mọi thao tác tay đều ghi "máy đọc gì -> sửa thành gì", ai, lúc nào | Tab Audit Log / web Admin |

## REST API + Swagger UI (api_server.py)
Giao diện web Admin kiểu HTML cũ (`admin_web.py`) đã được **thay bằng REST API được viết tài liệu bằng Swagger UI** (chuẩn OpenAPI 3).
Máy bảo vệ chạy `python app.py` như bình thường; app tự mở thêm API và hiện địa chỉ trên thanh trên cùng. Chạy riêng không cần giao diện: `python api_server.py`.

| Địa chỉ | Nội dung |
|---|---|
| `http://<IP máy bảo vệ>:8000/docs` | **Swagger UI**: đọc tài liệu và gọi thử từng API |
| `http://<IP máy bảo vệ>:8000/openapi.json` | Mô tả toàn bộ API dạng **.json** (OpenAPI) |
| `openapi.json` (cạnh `app.py`) | Bản đã xuất sẵn; tạo lại bằng `python export_openapi.py [file.json] [http://IP:8000]` |

**Cách thử trên Swagger UI:** bấm **Authorize** -> nhập `admin` / `admin123` (hoặc `baove` / `baove123`) -> **Try it out** -> **Execute**. Có thể nạp `openapi.json` vào Swagger Editor / Postman.

| Nhóm | API |
|---|---|
| Hệ thống | `GET /api/health` |
| Xác thực | `POST /api/auth/token` (form, cho nút Authorize), `POST /api/auth/login` (JSON), `GET /api/auth/me`, `POST /api/auth/logout`, `POST /api/auth/password` |
| Bãi xe | `GET /api/lot` (sơ đồ 3x3), `POST /api/lot/move` (di dời) |
| Cổng | `POST /api/gate/recognize` (ảnh -> biển), `POST /api/gate/scan` (check-in/out, cảnh báo vàng), `POST /api/gate/confirm-near`, `GET /api/gate/lost-ticket/candidates`, `POST /api/gate/lost-ticket` |
| Lượt gửi xe | `GET /api/history` (phân trang keyset + tìm biển), `PATCH /api/logs/{id}/plate`, `DELETE /api/logs/{id}` (hủy lượt nhập nhầm) |
| Vé tháng (ADMIN) | `GET/POST /api/subscriptions`, `POST /api/subscriptions/{id}/renew`, `DELETE /api/subscriptions/{id}` |
| Báo cáo (ADMIN) | `GET /api/stats/daily`, `GET /api/stats/performance`, `GET /api/audit`, `GET /api/export/revenue`, `GET /api/export/audit` (.xlsx) |

- Nghiệp vụ dùng lại `services.ParkingService` nên tính phí, vé tháng, Levenshtein <= 1, Audit Log giống hệt app Tkinter; hai máy dùng chung `parking.db`.
- Bảo vệ: Bearer token, hết phiên sau 30 phút; phân quyền GUARD / ADMIN; khóa tạm khi sai mật khẩu quá 5 lần / 5 phút (HTTP 429).
- Lần đầu Windows hỏi tường lửa -> chọn **Allow**. Tắt bằng `API_ENABLED = False` trong `config.py`; đổi cổng bằng `API_PORT`.
- **Chỉ dùng trong mạng nội bộ**, không đưa ra internet (chưa có HTTPS). Hãy **đổi mật khẩu mặc định** (`GET /api/auth/me` báo `using_default_password`).
- Web Admin HTML cũ vẫn còn trong `admin_web.py`, mặc định tắt (`ADMIN_WEB_ENABLED = False`, cổng 8001).
- Test API: `python -m pytest tests/test_api.py -q`.

## Chấm độ chính xác (evaluate.py)
Đặt ảnh biển ban ngày vào một thư mục, đặt tên file theo biển đúng (`59D1-123.45.jpg`, `30A12345__ban_ngay_2.png`) hoặc dùng `labels.csv` (cột `file,plate`), rồi chạy `python evaluate.py thu_muc_anh`.

## Vé tháng: nhận biết còn hạn / sắp hết / hết hạn
Admin thêm vé ở tab **Vé tháng** (biển số, chủ xe, từ ngày, đến ngày); chọn một vé rồi bấm **Gia hạn +30 ngày** để gia hạn. Bảng tô màu: đỏ = hết hạn, vàng = sắp hết (còn <= `SUB_WARN_DAYS` = 7 ngày).
Bảo vệ nhận biết vé tháng ở 4 chỗ:
1. **Trên video**: khi biển đã đọc chắc (khung xanh), dưới khung có nhãn `VE THANG con 23 ngay` (xanh dương), `... SAP HET HAN` (cam), `VE THANG HET HAN dd/mm/yyyy` (đỏ) hoặc `VE LE` (xám).
2. **Lúc quét vào/ra**: thanh trạng thái ghi rõ loại vé và số ngày còn lại. Vé hết hạn / chưa hiệu lực / sắp hết -> màu vàng, còi kêu; hết hạn còn hiện hộp thoại cảnh báo.
3. **Ô trong bãi 3x3**: ô xe vé tháng tô xanh nhạt ghi "VÉ THÁNG"; sắp hết tô vàng; hết hạn tô đỏ.
4. **Tính phí**: vé còn hạn ra miễn phí; hết hạn vẫn cho vào nhưng **thu phí vé lẻ khi ra** (loại phí EXPIRED_SUB, có ghi sự kiện SUB_EXPIRED trong `event_log`).

## Khi máy đọc sai biển (an toàn dữ liệu)
- Đọc **sai định dạng / thiếu ký tự** -> luôn hiện hộp nhập tay (còi kêu), không tự cho vào.
- Đọc **đúng định dạng nhưng tin cậy thấp** (< `AUTO_ACCEPT_CONF` trong `config.py`, mặc định 0.90) -> hiện hộp xác nhận, bảo vệ đối chiếu biển thật và sửa rồi mới cho vào/ra. Đặt `1.1` để LUÔN xác nhận, `0` để không hỏi.
- Lỡ cho vào với biển sai: bấm **"Sửa biển / Hủy lượt vào nhập nhầm..."** (hoặc chuột phải vào ô có xe).
  - **Sửa biển**: đổi biển của xe đang trong bãi (không cho trùng biển xe khác).
  - **Hủy lượt vào**: xóa lượt vào nhầm, ô trống lại, không thu phí. Bảo vệ hủy được trong `VOID_WINDOW_MIN` phút đầu (mặc định 10), Admin hủy bất cứ lúc nào.
  - Mọi lần sửa/hủy đều ghi vào bảng `event_log` (PLATE_CORRECTED, CHECKIN_VOIDED, OCR_CORRECTED) để đối soát.
- Dữ liệu lưu trong file `parking.db` (SQLite) cạnh `app.py`; xóa file này để làm lại từ đầu (mất toàn bộ lịch sử, vé tháng; tài khoản mặc định được tạo lại).

## Camera: USB trên máy tính + camera điện thoại (camera.py)
Ngay dưới video có ô chọn nguồn camera, bấm **Dùng camera** để chuyển (đổi qua lại lúc nào cũng được):
- **Camera USB (máy tính) - số 0 / số 1**: webcam hoặc camera USB. Không lên hình thì thử số kia.
- **Camera điện thoại (IP)**: cài app IP Webcam (Android) hoặc DroidCam, bấm *Start server*, rồi bấm **Địa chỉ ĐT...** nhập địa chỉ, ví dụ `192.168.1.5:8080`
  (app tự thêm `http://` và `/video`; link `rtsp://...` thì giữ nguyên). Điện thoại và máy tính phải **cùng Wi-Fi**. Địa chỉ được nhớ ở `storage/camera.json`.
- Hình được đọc ở luồng nền, chỉ giữ khung mới nhất nên không bị trễ; rớt mạng thì tự nối lại và báo trên thanh trạng thái; khung >1280px được thu nhỏ cho nhận diện nhanh.

## Nhận diện trực tiếp trên camera (live.py)
Màn hình Bảo vệ vẽ **khung quanh biển số + chữ nhỏ đọc được** ngay trên video:
- Khung **xanh** + biển dạng `59D1-123.45  93%`: đã có >=2 khung hình liên tiếp đọc giống nhau (chắc).
- Khung **cam** + dấu `?`: đọc được nhưng chưa chắc / chưa đúng định dạng.
- Nhận diện chạy ở luồng nền nên video không bị đơ; kết quả được **bỏ phiếu theo đa số** nhiều khung hình nên đỡ sai 1-2 ký tự.
- Bấm **QUÉT BIỂN** khi khung đang xanh -> dùng luôn biển đã xác nhận, không đọc lại.
- Tắt/bật bằng ô "Hiện khung nhận diện trực tiếp". Chế độ "Mở ảnh" cũng vẽ khung lên ảnh tĩnh.

## Dùng bộ nhận diện YOLO từ GitHub (khuyên dùng)
Chỉ thay phần nhận diện, app giữ nguyên. Chép **2 file trọng số** vào thư mục `models/`:
- `models/LP_detector.pt` - dò khung biển số
- `models/LP_ocr.pt` - dò từng ký tự trong biển (class = 0-9, A-Z)

Có đủ cả hai thì `AnprPipeline` tự dùng (`anpr_yolo.py`); thiếu một trong hai thì tự quay về RapidOCR + dò contour như cũ.
Cần `pip install yolov5` (cho trọng số YOLOv5 gốc) hoặc `ultralytics` (cho YOLOv8). Chạy `python debug_ocr.py anh.png` để kiểm tra.
Lưu ý: kiểm tra license của repo/dataset trước khi dùng cho đồ án, và ghi nguồn vào báo cáo.

## Dùng YOLO thật (cách cũ: chỉ thay bộ dò biển)
Chưa có `models/plate.pt` thì hệ thống dùng bộ dò khung chữ nhật của OpenCV (chỉ để thử nghiệm, kém ổn định
trên ảnh thực). Để có độ chính xác tốt: huấn luyện YOLOv8 trên dataset biển số (ví dụ trên Roboflow/Kaggle),
rồi chép trọng số vào `models/plate.pt`.

## Khi OCR đọc sai
1. Chạy `python debug_ocr.py duong_dan_anh.png`, xem thư mục `debug/`:
   - `1_detect.png`: khung xanh có ôm đúng biển không? Sai -> lỗi ở bước dò biển (cần `models/plate.pt`).
   - `2_crop.png`, `3_line*_*.png`: ảnh thực sự đưa vào OCR; in ra chữ đọc được của từng biến thể.
2. Biển 2 dòng được tách đôi và đọc riêng; mỗi dòng thử 4 biến thể ảnh rồi chọn tổ hợp đúng định dạng biển số.
3. Chỉnh `OCR_CONF` / `DETECT_CONF` trong `config.py`. Ảnh quay màn hình: giảm độ sáng, tắt chống chói, đặt camera thẳng.
