## 🤖 NATIVE AI WORKFLOW (GitHub Copilot Business)

Hệ thống vận hành theo kiến trúc 3 tầng bản địa (Single Source of Truth), lấy **Issue/PR** làm trung tâm điều phối và **Copilot Coding Agent** làm nơi thực thi. Tuyệt đối không sử dụng AI bên ngoài — **Zero-External AI** — để duy trì tính toàn vẹn của Copilot Memory Index.

---

### 1. Tầng Định hướng (Human)
* **Vai trò:** Supreme Commander (Chỉ huy tối cao).
* **Nhiệm vụ:** Tạo Issue mô tả Task → Tag `@github-copilot` hoặc Assign Issue cho Copilot → Ra quyết định `Merge` cuối cùng.
* **Không làm:** Không tự viết Prompt kỹ thuật, không copy-paste output AI, không can thiệp thủ công vào code.

### 2. Tầng Thiết kế & Kiểm duyệt (GitHub Web)
* **Architect (Anthropic Claude Opus 4.6):** Kích hoạt qua comment `@github-copilot` trên Issue/PR. Đọc toàn bộ repo từ Copilot Memory Index, phân tích yêu cầu và vạch ra Spec chi tiết (Implementation Plan).
* **Reviewer (OpenAI GPT-5.4):** Tự động kích hoạt qua PR Ruleset khi có PR mới hoặc push mới. Đối chiếu code với Spec, kiểm tra CodeQL alerts, cấp `APPROVED` hoặc `REQUEST_CHANGES`.
* **Cross-Inspector (Google Gemini 3.1 Pro):** Kích hoạt thủ công trên Web (Circuit Breaker) khi PR bị reject ≥3 lần hoặc có xung đột logic nghiêm trọng.

### 3. Tầng Thực thi (Copilot Coding Agent)
* **Developer (OpenAI GPT-5.2-Codex):** Kích hoạt bằng **Assign Issue cho Copilot** hoặc comment `@github-copilot` trên Issue. Agent tự đọc repo qua GitHub API (không qua IDE), tạo branch, sinh code, chạy CI và đẩy PR tự động. **Không dùng `@workspace` thủ công** — Agent đọc ngữ cảnh trực tiếp từ repo, đảm bảo context liền mạch.

### 4. Sơ đồ luồng CI/CD
```
Issue (Human tạo)
  │
  ├─► Architect (Opus 4.6) phân tích → viết Spec vào Issue comment
  │
  ├─► Assign Issue → Copilot Coding Agent (Codex 5.2)
  │     │
  │     ├─► Agent đọc repo + Spec → tạo branch → sinh code + test
  │     │
  │     └─► Agent tự push → PR tự động được tạo
  │           │
  │           ├─► CI Pipeline (GitHub Actions)
  │           │     ├── check_import_scope
  │           │     ├── check_signature
  │           │     ├── check_pr_scope
  │           │     ├── check_spec_lock
  │           │     └── Unit tests
  │           │
  │           ├─► Security Gates (Guard 4.9)
  │           │     ├── CodeQL Analysis (no high/critical)
  │           │     ├── Dependency Review (Dependabot)
  │           │     ├── Secret Scanning + Push Protection
  │           │     └── Copilot Autofix suggestions
  │           │
  │           ├─► Auto Review (GPT-5.4) → APPROVED / REQUEST_CHANGES
  │           │     │
  │           │     ├─► Nếu REQUEST_CHANGES: Agent tự đọc review → auto-fix → push lại
  │           │     │
  │           │     └─► Nếu reject ≥3 lần: Circuit Breaker → Gemini 3.1 Pro phân xử
  │           │
  │           └─► Human Merge (quyết định cuối cùng)
  │
  └─► ✅ Merge vào develop → main
```

### 5. Giao thức Kết nối & Xử lý Ngoại lệ (Hard Rules)

* **Rule 1 — Assign-to-Deploy (Giao việc = Triển khai):** Mọi task cho Developer (Codex) phải đi qua cơ chế **Assign Issue**. Human ghi Spec rõ ràng vào Issue body hoặc comment, sau đó Assign cho Copilot. Agent tự đọc Issue, đọc repo context qua API, tạo branch và PR. **Tuyệt đối không dùng `@workspace` thủ công trong IDE** — đây là nguyên nhân gây đứt gãy ngữ cảnh Web ↔ IDE.

* **Rule 2 — Auto-Fix Loop (Vòng lặp tự sửa):** Khi GPT-5.4 đánh `REQUEST_CHANGES`, Copilot Coding Agent tự đọc review comments trên PR và push bản sửa mới. Human **không** copy lỗi thủ công, **không** can thiệp vào IDE. Nếu Agent không tự fix được, Human comment hướng dẫn bổ sung trực tiếp trên PR.

* **Rule 3 — Circuit Breaker (Quy tắc quá tam ba bận):** Nếu PR bị `REQUEST_CHANGES` từ **3 lần trở lên** (bởi cùng reviewer hoặc cùng loại lỗi), quy trình REJECT tự động dừng. Human triệu hồi **Gemini 3.1 Pro** vào PR để phân xử độc lập, tìm nguyên nhân gốc rễ, và đề xuất mã code chốt hạ.

* **Rule 4 — Security Gate Enforcement (Cổng bảo mật bắt buộc):** Mọi PR phải vượt qua **4 cổng bảo mật** trước khi được phép Merge:
  1. **CodeQL:** Không có alert mức High hoặc Critical.
  2. **Dependency Review:** Dependabot không phát hiện vulnerability mức High+ chưa được xử lý.
  3. **Secret Scanning + Push Protection:** Không có secret bị rò rỉ. Push Protection chặn commit chứa secret.
  4. **Copilot Autofix:** Mọi suggestion từ Autofix phải được review (accept hoặc dismiss có lý do).

* **Rule 5 — CI Failure Recovery (Phục hồi lỗi CI):** Khi CI fail:
  1. Coding Agent tự đọc log lỗi từ GitHub Actions API và push bản sửa.
  2. Nếu fail lặp lại ≥2 lần cùng lỗi: Human đọc log, comment hướng dẫn cụ thể lên PR.
  3. Nếu fail do infrastructure (flaky test, runner issue): Human re-run workflow thủ công.
  4. Nếu fail do security gate: Xử lý theo Rule 4, **không** bypass bằng force-merge.

Hệ thống vận hành theo kiến trúc 3 tầng bản địa, lấy Pull Request (PR) và Issue làm trung tâm điều phối. Tuyệt đối không sử dụng AI bên ngoài (Zero-External AI) để duy trì tính toàn vẹn của Copilot Memory.

### 1. Tầng Định hướng (Human)
* **Vai trò:** Supreme Commander (Chỉ huy tối cao).
* **Nhiệm vụ:** Chỉ định Task qua Issue/PR, giao việc bằng tag `@github-copilot`, không tự viết Prompt kỹ thuật, ra quyết định `Merge` cuối cùng.

### 2. Tầng Thiết kế & Kiểm duyệt (GitHub Web)
* **Architect (Anthropic Claude Opus 4.6):** Kích hoạt qua comment trên giao diện Web. Đọc `AI_CONTEXT.md` từ Memory, phân tích Issue và vạch ra Spec (các bước thực thi chi tiết).
* **Reviewer (OpenAI GPT-5.4):** Tự động kích hoạt qua Ruleset khi có PR. Sử dụng dữ liệu phân tích từ CodeQL, đối chiếu với Spec để cấp `APPROVED` hoặc `REJECTED`.
* **Cross-Inspector (Google Gemini 3.1 Pro):** Kích hoạt thủ công trên Web khi có xung đột logic hoặc PR độ khó cao để thanh tra chéo (Cross-check) độc lập.

### 3. Tầng Thực thi (IDE / Copilot Workspace)
* **Developer (OpenAI GPT-5.2-Codex):** Kích hoạt bằng `@workspace` trong IDE. Nhận Spec từ Architect, tự động sinh code, refactor và đẩy (Push) thay đổi lên PR.

### 4. Giao thức Kết nối & Xử lý Ngoại lệ (Hard Rules)
* **Rule 1 - Định danh tuyệt đối (Absolute Targeting):** Mọi lệnh giao việc cho Developer (Codex) trong IDE bắt buộc phải gắn kèm ID của Issue/PR. Cú pháp chuẩn: `@workspace Thực thi Spec từ Issue #[ID] do Architect đã chốt`.
* **Rule 2 - Vòng lặp REJECT (Auto-Fix Loop):** Khi GPT-5.4 đánh `REJECTED`, Human tuyệt đối không copy lỗi thủ công. Human gõ lệnh vào IDE: `@workspace Đọc comment review mới nhất tại PR #[ID] và tự động sửa lỗi`.
* **Rule 3 - Quy tắc quá tam ba bận (Rule of Three):** Nếu PR bị GPT-5.4 `REJECTED` quá 3 lần vì cùng một lỗi, quy trình tự động dừng. Human triệu hồi **Gemini 3.1 Pro** vào PR đó để phân xử, tìm nguyên nhân gốc rễ và đưa ra mã code chốt hạ.
