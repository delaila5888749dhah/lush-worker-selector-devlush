# 🎁 Lush Worker Selector — Robot Mua Gift Card Tự Động

> **Tóm tắt 1 dòng:** Đây là robot chạy trên máy tính, tự động vào website [lushusa.givex.com](https://wwws-usa2.givex.com/cws4.0/lushusa/) để mua thẻ quà tặng điện tử (e-gift card) số lượng lớn, giả làm người thật để website không phát hiện ra là bot.

---

## 📌 Mục lục

1. [Dự án này làm gì?](#1-dự-án-này-làm-gì)
2. [Cần chuẩn bị những gì?](#2-cần-chuẩn-bị-những-gì-trước-khi-chạy)
3. [Các API & dịch vụ bên ngoài](#3-các-api--dịch-vụ-bên-ngoài)
4. [Điền API key ở đâu?](#4-điền-api-key-ở-đâu)
5. [Cài đặt từng bước](#5-cài-đặt-từng-bước)
6. [Cách chạy robot](#6-cách-chạy-robot)
7. [Cách kiểm tra robot có hoạt động không](#7-cách-kiểm-tra-robot-có-hoạt-động-không)
8. [Trạng thái sẵn sàng dùng thật](#8-trạng-thái-sẵn-sàng-dùng-thật)
9. [Cấu trúc thư mục](#9-cấu-trúc-thư-mục-tham-khảo)
10. [Khắc phục sự cố](#10-khắc-phục-sự-cố-thường-gặp)

---

## 1. Dự án này làm gì?

Robot này tự động thực hiện toàn bộ quy trình mua Gift Card trên website Lush USA:

- 🌐 Tự mở trình duyệt ẩn danh (mỗi lần mua dùng 1 trình duyệt khác nhau, có vân tay khác nhau)
- 🖱️ Tự di chuyển chuột giống người thật (đường cong, không đi thẳng)
- ⌨️ Tự gõ phím với tốc độ và nhịp độ của con người (có lỗi chính tả nhẹ, có ngập ngừng)
- 💳 Tự điền thông tin thẻ tín dụng và địa chỉ thanh toán
- ✅ Tự nhận kết quả: mua thành công / bị từ chối / phải xác thực 3D Secure
- 📱 Tự gửi kết quả về Telegram của bạn

**Có thể chạy nhiều con robot cùng lúc** (mặc định tối đa 10), mỗi con làm 1 đơn riêng biệt.

**Đối tượng dùng:** Người vận hành (operator), KHÔNG phải người dùng cuối.

---

## 2. Cần chuẩn bị những gì trước khi chạy?

Trước khi bắt đầu, bạn **BẮT BUỘC** phải có đủ 5 thứ sau:

| # | Cần có | Mục đích | Nơi lấy |
|---|--------|----------|---------|
| 1 | **Máy tính Windows/Mac/Linux** có cài Python 3.10+ | Để chạy robot | [python.org](https://www.python.org/) |
| 2 | **Phần mềm BitBrowser** (có bản quyền, đang mở) | Tạo trình duyệt ẩn danh chống phát hiện bot | [bitbrowser.net](https://www.bitbrowser.net/) |
| 3 | **Proxy tĩnh IP Mỹ** (SOCKS5 hoặc HTTP) | Để website thấy bạn đang ở Mỹ | Mua từ nhà cung cấp proxy |
| 4 | **License key MaxMind (miễn phí)** | Tải database IP → ZIP code (dùng offline) | Đăng ký tại [maxmind.com/en/geolite2/signup](https://www.maxmind.com/en/geolite2/signup) |
| 5 | **Bot Telegram** (tùy chọn nhưng nên có) | Nhận thông báo khi mua xong | Chat với [@BotFather](https://t.me/BotFather) trên Telegram |

Ngoài ra bạn cần chuẩn bị **dữ liệu đầu vào**:
- Danh sách billing profile (họ tên, địa chỉ, điện thoại, email) — định dạng file `.txt`
- Danh sách task: email người nhận | số tiền | thẻ | tháng hết hạn | năm hết hạn | CVV

---

## 3. Các API & dịch vụ bên ngoài

Robot chỉ gọi **4 dịch vụ bên ngoài**. Không dùng OpenAI, không dùng Stripe, không dùng Google.

| Dịch vụ | Biến môi trường | Bắt buộc? | Vai trò |
|---------|-----------------|-----------|---------|
| 🌐 **BitBrowser** | `BITBROWSER_API_KEY`, `BITBROWSER_ENDPOINT` | ✅ Bắt buộc | Mở/đóng trình duyệt chống phát hiện (chạy local trên máy bạn) |
| 📱 **Telegram Bot API** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | ⚠️ Tùy chọn | Gửi thông báo kết quả mua |
| 🗺️ **MaxMind GeoLite2** | `MAXMIND_LICENSE_KEY`, `GEOIP_DB_PATH` | ✅ Bắt buộc (lần đầu) | Tra cứu IP → ZIP code (tải 1 lần, dùng offline) |
| 🗄️ **Redis** | `REDIS_URL` | ⚠️ Tùy chọn | Chống mua trùng khi chạy nhiều tiến trình (nếu không có sẽ tự dùng file) |

---

## 4. Điền API key ở đâu?

👉 **Tất cả API key và cấu hình đều điền vào MỘT file duy nhất tên là `.env`** (nằm ở thư mục gốc của dự án).

### Bước 1: Tạo file `.env` từ mẫu

```bash
cp .env.example .env
```

### Bước 2: Mở file `.env` bằng Notepad (hoặc editor bất kỳ) và điền vào các ô sau:

```bash
# ============================
# 🔴 BẮT BUỘC ĐIỀN ĐỂ CHẠY THẬT
# ============================

# [1] Bật chế độ chạy thật. Để = 0 là chạy thử (không làm gì).
ENABLE_PRODUCTION_TASK_FN=1

# [2] BitBrowser — lấy API key trong phần mềm BitBrowser đang cài trên máy
BITBROWSER_API_KEY=dán_key_bitbrowser_vào_đây
BITBROWSER_ENDPOINT=http://127.0.0.1:54345
1. Mở BitBrowser GUI → tạo thủ công **N profile** (khuyến nghị
   `N ≥ WORKER_COUNT × 2`). Ghi lại từng profile ID.
2. Mở `.env` và điền:
   ```
BITBROWSER_POOL_MODE=1
BITBROWSER_PROFILE_IDS=abc123,def456,ghi789,jkl012,mno345

# [3] MaxMind — lấy license key sau khi đăng ký tài khoản free
MAXMIND_LICENSE_KEY=dán_key_maxmind_vào_đây
GEOIP_DB_PATH=data/GeoLite2-City.mmdb

# ============================
# 🟡 NÊN ĐIỀN (để nhận thông báo)
# ============================

# [4] Telegram — chat với @BotFather để tạo bot
TELEGRAM_ENABLED=1
TELEGRAM_BOT_TOKEN=dán_token_telegram_vào_đây
TELEGRAM_CHAT_ID=dán_chat_id_vào_đây

# ============================
# 🟢 TÙY CHỌN (có thể để nguyên mặc định)
# ============================

TASK_INPUT_FILE=tasks/input.txt
BILLING_POOL_DIR=billing_pool
MAX_WORKER_COUNT=10
WORKER_COUNT=1
REDIS_URL=                           # Để trống = dùng file, không cần Redis
```

### 📑 Bảng giải thích đầy đủ các biến

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `ENABLE_PRODUCTION_TASK_FN` | `0` | **QUAN TRỌNG**. `0` = chạy thử (an toàn, không mua gì). `1` = bật chế độ MUA THẬT |
| `TASK_INPUT_FILE` | `tasks/input.txt` | File chứa danh sách đơn cần mua |
| `TELEGRAM_ENABLED` | `0` | `1` = bật gửi thông báo Telegram |
| `TELEGRAM_BOT_TOKEN` | — | Token bot Telegram (từ @BotFather) |
| `TELEGRAM_CHAT_ID` | — | ID của chat/nhóm nhận thông báo |
| `TELEGRAM_ALERT_CHAT_ID` | *giống TELEGRAM_CHAT_ID* | Chat riêng cho cảnh báo khẩn |
| `TELEGRAM_RATE_LIMIT` | `5` | Số tin nhắn/giây tối đa |
| `BITBROWSER_API_KEY` | — | API key của BitBrowser |
| `BITBROWSER_ENDPOINT` | `http://127.0.0.1:54345` | URL của BitBrowser (thường là máy local) |
| `BILLING_POOL_DIR` | `billing_pool` | Thư mục chứa file profile billing |
| `GEOIP_DB_PATH` | `data/GeoLite2-City.mmdb` | Đường dẫn file MaxMind đã tải |
| `MAXMIND_LICENSE_KEY` | — | Key MaxMind (chỉ dùng lần đầu để tải DB) |
| `MAX_WORKER_COUNT` | `10` | Số robot chạy cùng lúc tối đa |
| `WORKER_COUNT` | `1` | Số robot khởi đầu |
| `REDIS_URL` | *(trống)* | URL Redis (để trống nếu không dùng) |
| `BILLING_CB_THRESHOLD` | `3` | Sau 3 lần billing fail → tạm dừng |
| `BILLING_CB_PAUSE` | `120` | Dừng 120 giây khi fail liên tục |
| `CDP_CALL_TIMEOUT_SECONDS` | `10` | Timeout cho mỗi lệnh trình duyệt |
| `PAYMENT_WATCHDOG_TIMEOUT_S` | `10` | Timeout riêng cho bước thanh toán |
| `ENABLE_RETRY_LOOP` | `1` | `1` = thử lại khi thẻ bị từ chối |
| `ENABLE_RETRY_UI_LOCK` | `1` | `1` = tự phục hồi khi trang bị khóa |
| `IDEMPOTENCY_STORE_PATH` | `.idempotency_store.json` | File chống mua trùng |

---

## 5. Cài đặt từng bước

### Bước 1 — Cài Python và tải code

```bash
# Tải code về
git clone https://github.com/1minhtaocompany/lush-worker-selector-devlush.git
cd lush-worker-selector-devlush

# Cài thư viện Python
pip install -r requirements.txt
```

*(Nếu dùng Anaconda thì thay bằng: `conda env create -f environment.yml`)*

### Bước 2 — Tạo file cấu hình `.env`

```bash
cp .env.example .env
```

Rồi mở `.env` và điền API key theo hướng dẫn ở [Mục 4](#4-điền-api-key-ở-đâu).

### Bước 3 — Tải database MaxMind (chỉ làm 1 lần)

```bash
# Thay <your-key> bằng MaxMind license key của bạn
MAXMIND_LICENSE_KEY=<your-key> python scripts/download_maxmind.py
```

Sau khi chạy xong, sẽ có file `data/GeoLite2-City.mmdb`.

### Bước 4 — Chuẩn bị dữ liệu billing

Tạo file `.txt` trong thư mục `billing_pool/`, mỗi dòng 1 profile, phân cách bằng dấu `|`:

```
first_name|last_name|address|city|state|zip|phone|email
John|Smith|123 Main St|Los Angeles|CA|90001|3105551234|john@example.com
Mary|Jones|456 Oak Ave|New York|NY|10001|2125559876|mary@example.com
```

(Hoặc chạy `python scripts/seed_billing_pool.py` để dùng dữ liệu mẫu.)

### Bước 5 — Chuẩn bị danh sách task

Tạo file `tasks/input.txt`, mỗi dòng là 1 đơn:

```
recipient@email.com|100|4111111111111111|07|27|123
another@email.com|50|5555555555554444|12|26|456
```

Format: `email_người_nhận | số_tiền | số_thẻ | tháng_hết_hạn | năm_hết_hạn | CVV`

### Bước 6 — Mở phần mềm BitBrowser

Đảm bảo BitBrowser đang chạy và lắng nghe tại `http://127.0.0.1:54345` (cổng mặc định).

---

## 6. Cách chạy robot

### 🟢 Chạy thử (an toàn — không mua gì, chỉ kiểm tra cấu hình)

```bash
python -m app
```

### 🔴 Chạy thật (mua Gift Card thật)

```bash
ENABLE_PRODUCTION_TASK_FN=1 python -m app
```

### 🎚️ Chạy thật giới hạn 3 robot cùng lúc

```bash
ENABLE_PRODUCTION_TASK_FN=1 MAX_WORKER_COUNT=3 python -m app
```

### 🛑 Cách dừng robot

Nhấn `Ctrl + C` — robot sẽ dừng an toàn (drain các worker đang chạy rồi thoát).

---

## 7. Cách kiểm tra robot có hoạt động không

### Kiểm tra qua HTTP

Mở trình duyệt, truy cập:

```
http://127.0.0.1:8080/health
```

Nếu thấy `{"status":"healthy"}` → robot đang chạy tốt.

### Chạy test tự động

```bash
make test              # Test nhanh (unit test)
make test-integration  # Test tích hợp
make test-all          # Test toàn bộ (~2000+ test case)
```

### Xem thông báo trên Telegram

Nếu đã bật `TELEGRAM_ENABLED=1`, mỗi lần mua xong sẽ nhận được tin nhắn trên Telegram kèm ảnh chụp màn hình (đã làm mờ số thẻ).

---

## 8. Trạng thái sẵn sàng dùng thật

### ✅ Đã sẵn sàng production

- **Không có mock/fake** trong luồng chính. Toàn bộ code kết nối BitBrowser, Givex, Telegram, MaxMind đều là thật.
- **2061 test case đều pass** (lần chạy gần nhất 2026-04-23).
- Có đầy đủ: retry khi thẻ bị từ chối, xử lý 3D Secure, chống mua trùng, circuit breaker, health check.

### ⚠️ Cần lưu ý

- Biến `ENABLE_PRODUCTION_TASK_FN` **mặc định = 0** (chế độ thử). Bạn **BẮT BUỘC phải đặt = 1** để chạy thật.
- Biến `TELEGRAM_ENABLED` **mặc định = 0**. Bật lên bằng `=1` nếu muốn nhận thông báo.
- BitBrowser phải **đang mở** trước khi khởi động robot.
- Proxy tĩnh IP Mỹ phải được cấu hình trong BitBrowser hoặc `PROXY_LIST_FILE`.

### 📌 Về `NotImplementedError` trong code

Trong file `integration/orchestrator.py` có 9 chỗ `raise NotImplementedError` — **KHÔNG phải bug hay chưa làm**. Đó là class trừu tượng (abstract base class) `_IdempotencyStore`, có 2 class con đã implement đầy đủ:
- `_FileIdempotencyStore` (dùng khi không có Redis)
- `_RedisIdempotencyStore` (dùng khi có `REDIS_URL`)

Đây là mẫu thiết kế OOP chuẩn.

---

## 9. Cấu trúc thư mục (tham khảo)

```
lush-worker-selector/
├── .env.example         👈 File mẫu cấu hình — COPY thành .env để dùng
├── app/                 👈 Điểm khởi động (python -m app)
├── billing_pool/        👈 Đặt file .txt chứa profile billing ở đây
├── tasks/               👈 Đặt file input.txt chứa danh sách đơn ở đây
├── modules/             📦 Các module nghiệp vụ
│   ├── cdp/             — Điều khiển trình duyệt (BitBrowser, Selenium)
│   ├── billing/         — Quản lý profile billing
│   ├── fsm/             — Logic phân loại kết quả thanh toán
│   ├── notification/    — Gửi Telegram
│   ├── rollout/         — Tăng/giảm số worker tự động
│   ├── watchdog/        — Timeout và cứu hộ
│   ├── observability/   — Health check, metrics, logging
│   └── delay/           — Giả lập nhịp điệu con người
├── integration/         🔗 Điều phối các module (orchestrator, runtime)
├── scripts/             🛠️ Công cụ dòng lệnh (download MaxMind, seed billing)
├── ci/                  🔍 Kiểm tra chất lượng code
├── docs/                📚 Tài liệu vận hành
├── spec/                📐 Đặc tả kỹ thuật
└── tests/               🧪 ~2061 test case
```

---

## 10. Khắc phục sự cố thường gặp

| Lỗi | Nguyên nhân | Cách xử lý |
|-----|-------------|------------|
| `BITBROWSER_API_KEY is required` | Chưa điền key trong `.env` | Mở `.env` điền `BITBROWSER_API_KEY=...` |
| `Cannot connect to 127.0.0.1:54345` | BitBrowser chưa mở | Mở phần mềm BitBrowser lên |
| `GeoLite2-City.mmdb not found` | Chưa tải MaxMind DB | Chạy `python scripts/download_maxmind.py` |
| `pre-flight geo check failed` | Proxy không phải IP Mỹ | Kiểm tra proxy trong BitBrowser |
| `No billing profiles loaded` | Thư mục `billing_pool/` trống | Thêm file `.txt` vào `billing_pool/` |
| `No tasks in input.txt` | File `tasks/input.txt` trống | Thêm dòng task vào file |
| Robot chạy nhưng không mua gì | `ENABLE_PRODUCTION_TASK_FN=0` | Đổi thành `ENABLE_PRODUCTION_TASK_FN=1` |
| Không nhận được Telegram | `TELEGRAM_ENABLED=0` | Đổi thành `TELEGRAM_ENABLED=1` và điền token |

### Xem log chi tiết

Log được ghi ra màn hình và file. Số thẻ, CVV, email đều được **che tự động** (PII redaction) nên có thể chia sẻ log mà không lộ dữ liệu nhạy cảm.

---

## 📞 Hỗ trợ thêm

- Tài liệu vận hành chi tiết: `docs/operations/RUNBOOK.md`
- Kế hoạch triển khai canary: `docs/canary_rollout.md`
- Kế hoạch rollback khẩn cấp: `docs/rollback.md`
- Cách bàn giao cho người mới: `docs/HANDOVER.md`

---

**Phiên bản README:** 2026-04-24 · **Trạng thái:** Production-ready ✅
