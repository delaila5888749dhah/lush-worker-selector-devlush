# 🎁 Lush Worker Selector — Robot Mua Gift Card Tự Động

> **Tóm tắt 1 dòng:** Robot chạy trên máy tính, tự động vào website [lushusa.givex.com](https://wwws-usa2.givex.com/cws4.0/lushusa/) để mua thẻ quà tặng điện tử (Gift Card) với hành vi mô phỏng người dùng thật.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Tests](https://img.shields.io/badge/tests-2403%2B%20passing-brightgreen.svg)
![Status](https://img.shields.io/badge/status-production--ready-success.svg)
![Docker](https://img.shields.io/badge/docker-ready-blue.svg)

---

## 📌 Mục lục

1. [Dự án này làm gì?](#1-dự-án-này-làm-gì)
2. [Kiến trúc tổng quan](#2-kiến-trúc-tổng-quan)
3. [Yêu cầu hệ thống](#3-yêu-cầu-hệ-thống)
4. [Cài đặt](#4-cài-đặt)
5. [Cấu hình `.env`](#5-cấu-hình-env)
6. [Chuẩn bị dữ liệu đầu vào](#6-chuẩn-bị-dữ-liệu-đầu-vào)
7. [Cách chạy robot](#7-cách-chạy-robot)
8. [Kiểm tra & Testing](#8-kiểm-tra--testing)
9. [Observability (giám sát)](#9-observability-giám-sát)
10. [CI/CD & Quality gates](#10-cicd--quality-gates)
11. [Cấu trúc thư mục](#11-cấu-trúc-thư-mục)
12. [Trạng thái sẵn sàng dùng thật](#12-trạng-thái-sẵn-sàng-dùng-thật)
13. [Khắc phục sự cố](#13-khắc-phục-sự-cố-thường-gặp)
14. [Đóng góp (Contributing)](#14-đóng-góp-contributing)
15. [License](#15-license)
16. [Tài liệu thêm](#16-tài-liệu-thêm)

> 🇬🇧 **For English speakers:** see [`docs/HANDOVER.md`](docs/HANDOVER.md) for the English on-call handover guide.

---

## 1. Dự án này làm gì?

Robot tự động hóa toàn bộ quy trình mua Gift Card trên Lush USA:

- 🌐 Tự mở trình duyệt ẩn danh qua **BitBrowser** (mỗi đơn = 1 fingerprint khác nhau)
- 🖱️ Tự di chuyển chuột theo đường cong **Bézier** giống người thật
- ⌨️ Tự gõ phím với nhịp con người (có hesitation, có drift AR(1))
- 💳 Tự điền thông tin thẻ tín dụng và địa chỉ thanh toán
- ✅ Tự phân loại kết quả qua **FSM**: `success / declined / 3DS / ui_lock`
- 🔁 Tự retry khi thẻ bị từ chối hoặc trang bị khóa
- 🛡️ Chống mua trùng (**idempotency** qua Redis hoặc file JSON)
- 📱 Tự gửi kết quả về **Telegram** kèm screenshot (đã làm mờ PII)
- 📊 Có **health check, metrics, alerting, log sink** sẵn sàng cho production

**Có thể chạy nhiều worker cùng lúc** (mặc định ≤ 10) với autoscaler, circuit breaker và canary rollout.

**Đối tượng dùng:** Operator vận hành — KHÔNG phải dịch vụ công cộng hay API mở.

---

## 2. Kiến trúc tổng quan

```
┌────────────────────────────────────────────────────────────────────┐
│                       app/ (entry: python -m app)                  │
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
                ┌─────────────────▼──────────────────┐
                │  integration/ (orchestrator,       │
                │   runtime, worker_task)            │
                └─────────────────┬──────────────────┘
   ┌───────────┬──────────┬───────┴────────┬───────────┬──────────┐
   ▼           ▼          ▼                ▼           ▼          ▼
modules/    modules/   modules/         modules/    modules/   modules/
 cdp/       billing/    fsm/            delay/     behavior/  rollout/
(BitBrowser (round-     (success/       (Bézier,   (decision  (autoscaler
 + Selenium  robin +    declined/       human      engine)     + canary)
 + MaxMind)  idem)      3DS/ui_lock)    typing)
   │           │          │                                       │
   ▼           ▼          ▼                                       ▼
modules/notification/  modules/watchdog/  modules/observability/  modules/monitor/
 (Telegram + queue)    (timeout & rescue) (health, metrics,       (transient
                                          alerts, log sink)        monitor)
```

Chi tiết: xem [`docs/HANDOVER.md`](docs/HANDOVER.md) và [`spec/blueprint.md`](spec/blueprint.md).

---

## 3. Yêu cầu hệ thống

| # | Cần có | Mục đích | Nơi lấy |
|---|--------|----------|---------|
| 1 | **Python 3.11+** *(bắt buộc, không hỗ trợ 3.10)* | Để chạy robot | [python.org](https://www.python.org/) |
| 2 | **BitBrowser** (có bản quyền, đang mở) | Trình duyệt anti-fingerprint | [bitbrowser.net](https://www.bitbrowser.net/) |
| 3 | **Proxy tĩnh IP Mỹ** (SOCKS5 hoặc HTTP) | Để Lush thấy bạn ở Mỹ | Mua từ nhà cung cấp proxy |
| 4 | **License key MaxMind** (miễn phí) | DB IP → ZIP code (offline) | [maxmind.com/en/geolite2/signup](https://www.maxmind.com/en/geolite2/signup) |
| 5 | **Bot Telegram** (khuyến nghị) | Nhận thông báo kết quả | [@BotFather](https://t.me/BotFather) |
| 6 | **Docker** (tùy chọn) | Triển khai container | [docker.com](https://www.docker.com/) |
| 7 | **Redis** (tùy chọn) | Idempotency multi-process | Local hoặc managed |

> ⚠️ Repo cấu hình `python_version = "3.11"` trong `pyproject.toml`, Dockerfile dùng `python:3.11-slim`, CI chạy Python 3.11. **Phải dùng Python 3.11 trở lên**.

---

## 4. Cài đặt

### 4.1. Cài thủ công bằng `pip`

```bash
git clone https://github.com/1minhtaocompany/lush-worker-selector-devlush.git
cd lush-worker-selector-devlush

# Khuyến nghị tạo virtualenv
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 4.2. Cài bằng Conda

```bash
conda env create -f environment.yml
conda activate lush-worker
```

### 4.3. Cài bằng Docker (khuyến nghị production)

```bash
# Build image
docker build -t lush-worker:latest .

# Run (mount .env và mở port health)
docker run --rm \
  --env-file .env \
  -p 8080:8080 \
  -v "$(pwd)/billing_pool:/app/billing_pool" \
  -v "$(pwd)/tasks:/app/tasks" \
  -v "$(pwd)/data:/app/data" \
  lush-worker:latest
```

Container đã có sẵn Chromium + chromium-driver, chạy non-root user `worker`, có `HEALTHCHECK` trỏ tới `/health`.

---

## 5. Cấu hình `.env`

👉 Tất cả API key & cấu hình điền vào một file duy nhất `.env` ở thư mục gốc.

```bash
cp .env.example .env
```

### 5.1. Bảng biến môi trường đầy đủ

#### 🔴 Bắt buộc

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `ENABLE_PRODUCTION_TASK_FN` | `0` | `0` = chạy thử (an toàn). **`1` = MUA THẬT** |
| `BITBROWSER_API_KEY` | — | API key BitBrowser |
| `BITBROWSER_ENDPOINT` | `http://127.0.0.1:54345` | URL BitBrowser local |
| `MAXMIND_LICENSE_KEY` | — | Key MaxMind (chỉ dùng lần đầu để tải DB) |
| `GEOIP_DB_PATH` | `data/GeoLite2-City.mmdb` | Đường dẫn file `.mmdb` |
| `TASK_INPUT_FILE` | `tasks/input.txt` | File chứa danh sách đơn |

#### ⚙️ Worker pool & runtime

| Biến | Mặc định | Range | Ý nghĩa |
|------|----------|-------|---------|
| `MAX_WORKER_COUNT` | `10` | `[1, 500]` *(WARN nếu >100)* | Trần worker tuyệt đối |
| `WORKER_COUNT` | `1` | `≤ MAX_WORKER_COUNT` | Số worker khởi đầu |
| `CDP_EXECUTOR_MAX_WORKERS` | *(auto)* | — | Thread pool cho CDP |
| `CDP_CALL_TIMEOUT_SECONDS` | **`15.0`** | `> 0` | Timeout mỗi lệnh CDP |
| `PAYMENT_WATCHDOG_TIMEOUT_S` | **`60`** | `> 0` | Timeout bước thanh toán |
| `ALLOW_DOM_ONLY_WATCHDOG` | `0` | `0/1` | Fallback khi Selenium thiếu CDP listener |
| `ENABLE_RETRY_LOOP` | `1` | `0/1` | Retry khi declined |
| `ENABLE_RETRY_UI_LOCK` | `1` | `0/1` | Retry khi `ui_lock` |

#### 🌐 BitBrowser pool & proxy

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `BITBROWSER_POOL_MODE` | `0` | `1` = dùng pool profile có sẵn (chống Operation Password) |
| `BITBROWSER_PROFILE_IDS` | — | CSV ID profile (bắt buộc khi `POOL_MODE=1`). Khuyến nghị `N ≥ WORKER_COUNT × 2` |
| `BITBROWSER_RETRY_ATTEMPTS` | `3` | Số lần thử lại API BitBrowser |
| `BITBROWSER_RETRY_WAIT_INITIAL_S` | `0.5` | Backoff khởi đầu (s) |
| `BITBROWSER_RETRY_WAIT_MAX_S` | `8.0` | Backoff tối đa (s) |
| `PROXY_SERVER` | — | Proxy outbound (`http://user:pass@host:port`) |

#### 💳 Billing & circuit breaker

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `BILLING_POOL_DIR` | `billing_pool` | Thư mục chứa file profile `.txt` (có thể là Google Drive Desktop path) |
| `MIN_BILLING_PROFILES` | `0` *(prod ép ≥1)* | Abort startup nếu pool nhỏ hơn |
| `MAX_BILLING_PROFILES` | `10000` | Giới hạn profile load vào RAM |
| `BILLING_CB_THRESHOLD` | `3` | Số fail liên tiếp trước khi trip CB |
| `BILLING_CB_PAUSE` | `120` | Giây dừng khi CB trip |
| `IDEMPOTENCY_STORE_PATH` | `.idempotency_store.json` | File chống mua trùng (khi không có Redis) |
| `REDIS_URL` | — | URL Redis (multi-process idempotency) |

#### 📱 Telegram

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `TELEGRAM_ENABLED` | `0` | Bật/tắt thông báo |
| `TELEGRAM_BOT_TOKEN` | — | Token từ @BotFather |
| `TELEGRAM_CHAT_ID` | — | ID chat/nhóm nhận thông báo |
| `TELEGRAM_ALERT_CHAT_ID` | *= TELEGRAM_CHAT_ID* | Chat riêng cho cảnh báo khẩn |
| `TELEGRAM_RATE_LIMIT` | `5` | Tin nhắn/giây |
| `TELEGRAM_VERBOSE` | `0` | Bật ping observability chi tiết |
| `TELEGRAM_PENDING_FILE` | `telegram_pending.jsonl` | File JSONL lưu payload fail để retry |

#### 🗺️ MaxMind & GeoIP

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `MAXMIND_DB_PATH` | — | Alias legacy của `GEOIP_DB_PATH` |
| `MAXMIND_RELOAD_INTERVAL_HOURS` | `24` | Background thread tự **hot-swap** `.mmdb` khi file thay đổi (không cần restart) |

#### 🧪 Givex URL overrides (staging)

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `GIVEX_ENDPOINT` | — | Override endpoint Givex |
| `GIVEX_PAYMENT_URL` | — | Override URL thanh toán |
| `GIVEX_EGIFT_URL` | — | Override URL e-gift |
| `ALLOW_NON_PROD_GIVEX_HOSTS` | `0` | **CHỈ** dùng staging. KHÔNG bật trong production |
| `GIVEX_GREETINGS_FILE` | — | File UTF-8 thêm greeting tùy biến (≤1000 dòng) |

#### 🎭 Hành vi & delay

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `DELAY_MAX_TYPING_DELAY` | `0.6` | Trần delay/keystroke (s) |
| `DELAY_MAX_HESITATION_DELAY` | `2.0` | Trần hesitation pause (s) |
| `DELAY_MAX_STEP_DELAY` | `8.0` | Trần delay giữa các step (s) |
| `DELAY_WATCHDOG_HEADROOM` | `5.0` | Headroom watchdog (s) |
| `ENABLE_GRADUAL_DRIFT` | `1` | AR(1) drift ±30% trên typing/thinking |
| `ENFORCE_CDP_TYPING_STRICT` | `1` | Cấm fallback `send_keys` (production default) |

#### 🪟 Popup handling

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `POPUP_USE_XPATH` | `0` | Dùng XPath thay CSS cho close button |
| `POPUP_CLEAR_AFTER_CLOSE` | `1` | Clear sentinel sau khi close thành công |
| `POPUP_CLOSE_MAX_RETRIES` | `3` | Số lần thử close popup |

#### 🧬 Khác

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `FSM_ALLOW_LEGACY` | `0` | Chỉ bật cho legacy-api tests |

### 5.2. BitBrowser Pool Mode (Blueprint §2.1)

Khi BitBrowser bật **Operation Password**, API `create/delete profile` bị chặn. Pool Mode giải quyết bằng cách dùng lại profile đã tạo sẵn:

1. Mở BitBrowser GUI → tạo thủ công **N profile** (khuyến nghị `N ≥ WORKER_COUNT × 2`). Ghi lại từng profile ID.
2. Trong `.env`:
   ```dotenv
   BITBROWSER_POOL_MODE=1
   BITBROWSER_PROFILE_IDS=abc123,def456,ghi789,jkl012,mno345
   ```
3. Chạy `python -m app` như bình thường. Bot sẽ:
   - Chọn profile **round-robin tuần tự** (không random)
   - Gọi `/browser/update/partial` để **random fingerprint** mỗi cycle
   - `/browser/open` → làm việc → `/browser/close` (KHÔNG delete)
4. Quay lại chế độ cũ: `BITBROWSER_POOL_MODE=0`. Hành vi legacy được giữ nguyên 100%.

Chi tiết & xử lý sự cố: [`docs/operations/RUNBOOK.md`](docs/operations/RUNBOOK.md) §2.4.

### 5.3. MaxMind auto-reload

Background thread tự kiểm tra mtime của `.mmdb` mỗi `MAXMIND_RELOAD_INTERVAL_HOURS` giờ và **hot-swap reader in-process** khi file thay đổi. Cron job có thể chạy `scripts/download_maxmind.py` hàng tháng mà **không cần restart robot**.

---

## 6. Chuẩn bị dữ liệu đầu vào

### 6.1. Tải MaxMind DB (chỉ làm 1 lần)

```bash
MAXMIND_LICENSE_KEY=<your-key> python scripts/download_maxmind.py
```

Sau khi chạy: có file `data/GeoLite2-City.mmdb` (đã verify SHA256).

### 6.2. Billing profiles

Tạo file `.txt` trong `billing_pool/`, mỗi dòng 1 profile, phân cách bằng `|`:

```
first_name|last_name|address|city|state|zip|phone|email
John|Smith|123 Main St|Los Angeles|CA|90001|3105551234|john@example.com
Mary|Jones|456 Oak Ave|New York|NY|10001|2125559876|mary@example.com
```

Hoặc seed từ file CSV/TSV (1000 profiles/file output):

```bash
python scripts/seed_billing_pool.py --input profiles.csv --output billing_pool/
```

> Input CSV cần tối thiểu 6 trường: `first_name,last_name,address,city,state,zip[,phone][,email]`.

### 6.3. Danh sách task

Tạo file `tasks/input.txt`, mỗi dòng = 1 đơn:

```
recipient@email.com|100|4111111111111111|07|27|123
another@email.com|50|5555555555554444|12|26|456
```

Format: `email_người_nhận | số_tiền | số_thẻ | tháng_hết_hạn | năm_hết_hạn | CVV[|card2|m|y|cvv2|...]`

### 6.4. Mở BitBrowser

Đảm bảo BitBrowser đang chạy và lắng nghe tại `BITBROWSER_ENDPOINT` (mặc định `http://127.0.0.1:54345`).

---

## 7. Cách chạy robot

### 🟢 Chạy thử (an toàn — không mua gì)

```bash
python -m app
```

### 🔴 Chạy thật

```bash
ENABLE_PRODUCTION_TASK_FN=1 python -m app
```

### 🎚️ Chạy thật giới hạn 3 worker

```bash
ENABLE_PRODUCTION_TASK_FN=1 MAX_WORKER_COUNT=3 python -m app
```

### 🐳 Chạy bằng Docker

```bash
docker run --rm --env-file .env -p 8080:8080 lush-worker:latest
```

### 🛑 Cách dừng

Nhấn `Ctrl + C` — robot drain các worker đang chạy rồi thoát an toàn.

---

## 8. Kiểm tra & Testing

### 8.1. Test suite (2403+ test cases)

```bash
make test              # Unit test (mặc định)
make test-integration  # Integration suite (L3 harness + L4 smoke)
make test-e2e          # 14 E2E tests (T-01 … T-14, tách riêng)
make test-all          # Tất cả: unit + integration + e2e
```

### 8.2. Quality gates

```bash
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy app modules integration
make coverage    # coverage report + coverage.xml
make audit       # pip-audit -r requirements.txt --strict
```

### 8.3. Kiểm tra qua HTTP

```bash
curl http://127.0.0.1:8080/health
# {"status":"healthy"}
```

### 8.4. Telegram

Khi bật `TELEGRAM_ENABLED=1`, mỗi cycle xong sẽ có tin nhắn kèm screenshot (PII đã được redact).

---

## 9. Observability (giám sát)

| Endpoint / kênh | Mô tả |
|-----------------|-------|
| `GET /health` (port 8080) | Liveness & readiness probe |
| Metrics exporter | Counter, gauge, histogram theo cycle/FSM state |
| Alerting sink | Cảnh báo ngưỡng (qua Telegram alert chat) |
| Log sink | Structured logs với **PII redaction** tự động (card, CVV, email) |

Chi tiết các endpoint, format metrics, alert thresholds: xem [`docs/HANDOVER.md`](docs/HANDOVER.md) và `modules/observability/`.

---

## 10. CI/CD & Quality gates

Repo có **5 GitHub Actions workflows** (`.github/workflows/`):

| Workflow | Mục đích |
|----------|----------|
| `ci.yml` | Lint (ruff) + typecheck (mypy) + test + pip-audit |
| `blueprint_contracts.yml` | Verify blueprint contracts trong `spec/contracts/` |
| `codeql.yml` | CodeQL security scan |
| `chaos-audit.yml` | Chaos / fault-injection audit |
| `smoke-real.yml` | Real-stack smoke test |

**Required checks** trước khi merge: ci, blueprint-contracts, codeql.

Labels đặc biệt:
- `approved-override` — bypass blueprint contract khi cần (cần justification)
- `blueprint-contracts` — PR có chạm contract files

Chi tiết quy trình PR & CHANGE_CLASS policy: xem [`spec/contracts/README.md`](spec/contracts/README.md).

---

## 11. Cấu trúc thư mục

```
lush-worker-selector-devlush/
├── .env.example         👈 Mẫu cấu hình — copy thành .env
├── Dockerfile           🐳 Image production (python:3.11-slim + Chromium)
├── Makefile             🛠️ Test + quality targets
├── requirements.txt     📦 Dependencies
├── requirements-lock.txt🔒 Pinned hashes cho Docker reproducibility
├── pyproject.toml       ⚙️ Cấu hình ruff/mypy/pytest
├── environment.yml      🐍 Conda env (tùy chọn)
│
├── app/                 🚀 Entry point (python -m app)
├── billing_pool/        💳 File .txt billing profiles
├── tasks/               📋 input.txt — danh sách đơn
├── data/                🗺️ MaxMind .mmdb (tải về)
│
├── modules/             📦 Module nghiệp vụ
│   ├── cdp/             — Điều khiển trình duyệt (BitBrowser, Selenium, MaxMind)
│   ├── billing/         — Quản lý profile + idempotency
│   ├── fsm/             — Phân loại kết quả thanh toán
│   ├── notification/    — Telegram bot + queue
│   ├── rollout/         — Autoscaler + canary + circuit breaker
│   ├── watchdog/        — Timeout & rescue
│   ├── observability/   — Health, metrics, alerts, log sink
│   ├── delay/           — Bézier mouse + human typing rhythm
│   ├── behavior/        — Behavioral decision engine
│   ├── monitor/         — Transient monitor
│   └── common/          — Shared utilities
│
├── integration/         🔗 Orchestrator + runtime + worker_task
├── scripts/             🛠️ download_maxmind.py, seed_billing_pool.py
├── ci/                  🔍 Code quality scripts
├── docs/                📚 RUNBOOK, HANDOVER, SECURITY, CHANGELOG, canary, rollback
├── spec/                📐 blueprint.md, audit-lock.md, contracts/
└── tests/               🧪 2403+ test cases (unit + integration + e2e)
```

---

## 12. Trạng thái sẵn sàng dùng thật

### ✅ Đã sẵn sàng production

- **Không có mock/fake** trong luồng chính. BitBrowser, Givex, Telegram, MaxMind đều là thật.
- **2403+ test cases pass** *(lần chạy gần nhất: 2026-04-28)*.
- Đầy đủ: retry declined, xử lý 3D Secure, idempotency, circuit breaker, autoscaler, canary rollout, health, metrics, alerts.
- Docker image reproducible với `--require-hashes`.
- CI: ruff + mypy + pip-audit + CodeQL + chaos audit.

### ⚠️ Cần lưu ý

- `ENABLE_PRODUCTION_TASK_FN` mặc định `=0`. **Phải đặt `=1`** để mua thật.
- `TELEGRAM_ENABLED` mặc định `=0`. Bật `=1` nếu cần thông báo.
- BitBrowser **phải đang chạy** trước khi khởi động robot.
- Proxy IP Mỹ phải cấu hình trong BitBrowser hoặc `PROXY_SERVER`.
- `ALLOW_NON_PROD_GIVEX_HOSTS=1` **CHỈ** cho staging.

### 📌 Về `NotImplementedError` trong code

Trong `integration/orchestrator.py` có 9 chỗ `raise NotImplementedError` — **KHÔNG phải bug**. Đó là abstract base class `_IdempotencyStore`, được hiện thực bởi:
- `_FileIdempotencyStore` (khi không có Redis)
- `_RedisIdempotencyStore` (khi có `REDIS_URL`)

Đây là OOP pattern chuẩn.

---

## 13. Khắc phục sự cố thường gặp

| Lỗi | Nguyên nhân | Cách xử lý |
|-----|-------------|------------|
| `BITBROWSER_API_KEY is required` | Chưa điền key | Mở `.env` điền `BITBROWSER_API_KEY=...` |
| `Cannot connect to 127.0.0.1:54345` | BitBrowser chưa mở | Mở phần mềm BitBrowser |
| `Operation Password blocked` | BitBrowser bật mật khẩu thao tác | Bật `BITBROWSER_POOL_MODE=1` + điền `BITBROWSER_PROFILE_IDS` |
| `GeoLite2-City.mmdb not found` | Chưa tải MaxMind DB | Chạy `scripts/download_maxmind.py` |
| `pre-flight geo check failed` | Proxy không phải IP Mỹ | Kiểm tra proxy / `PROXY_SERVER` |
| `No billing profiles loaded` | `billing_pool/` trống | Thêm file `.txt` hoặc seed bằng `seed_billing_pool.py` |
| `MIN_BILLING_PROFILES not satisfied` | Pool nhỏ hơn ngưỡng | Tăng pool hoặc giảm `MIN_BILLING_PROFILES` |
| `No tasks in input.txt` | File trống | Thêm dòng task |
| Robot chạy nhưng không mua | `ENABLE_PRODUCTION_TASK_FN=0` | Đặt `=1` |
| Không nhận Telegram | `TELEGRAM_ENABLED=0` | Đặt `=1` + điền token + chat_id |
| `Payment watchdog timeout` | Trang thanh toán treo | Tăng `PAYMENT_WATCHDOG_TIMEOUT_S` (mặc định 60s) |
| `CDP call timeout` | Mạng/proxy chậm | Tăng `CDP_CALL_TIMEOUT_SECONDS` (mặc định 15s) |

### Xem log

Log ra stdout & file. **Số thẻ, CVV, email tự động bị che (PII redaction)** nên có thể chia sẻ log an toàn.

---

## 14. Đóng góp (Contributing)

1. Fork repo, tạo branch từ `main`.
2. Trước khi commit, chạy đủ:
   ```bash
   make lint && make typecheck && make test-all && make audit
   ```
3. Nếu PR chạm `spec/contracts/`, gắn label `blueprint-contracts`.
4. Nếu cần bypass blueprint contract, gắn label `approved-override` + ghi rõ lý do trong PR description.
5. CI bắt buộc pass: `ci`, `blueprint-contracts`, `codeql`.

Chi tiết:
- [`spec/blueprint.md`](spec/blueprint.md) — Source of truth kiến trúc
- [`spec/contracts/README.md`](spec/contracts/README.md) — Cách thêm contract
- [`docs/SECURITY.md`](docs/SECURITY.md) — Security policy
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — Lịch sử thay đổi

---

## 15. License

Proprietary — © 2026 1MinhTaoCompany. Mọi quyền được bảo lưu.

> Dự án này là **operator tool nội bộ**. Không phân phối công khai. Liên hệ chủ sở hữu repo trước khi sử dụng/sao chép.

---

## 16. Tài liệu thêm

| Tài liệu | Mô tả |
|----------|-------|
| [`docs/operations/RUNBOOK.md`](docs/operations/RUNBOOK.md) | Vận hành chi tiết, on-call procedures |
| [`docs/HANDOVER.md`](docs/HANDOVER.md) | Bàn giao cho người mới (English) |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Security policy & vulnerability reporting |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | Changelog đầy đủ |
| [`docs/canary_rollout.md`](docs/canary_rollout.md) | Kế hoạch triển khai canary |
| [`docs/rollback.md`](docs/rollback.md) | Kế hoạch rollback khẩn cấp |
| [`spec/blueprint.md`](spec/blueprint.md) | Blueprint kiến trúc (source of truth) |
| [`spec/audit-lock.md`](spec/audit-lock.md) | Audit lock & change control |
| [`spec/contracts/`](spec/contracts/) | Blueprint contracts |
| [`scripts/README.md`](scripts/README.md) | Hướng dẫn các CLI scripts |

---

**Phiên bản README:** 2026-04-29 · **Trạng thái:** Production-ready ✅ · **Tests:** 2403+ passing
