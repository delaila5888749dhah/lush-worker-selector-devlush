SPEC‑6‑FINAL‑EXECUTION‑WORKFLOW

Phiên bản hoàn chỉnh – sẵn sàng triển khai

---

1. Nguyên tắc nền tảng

1. Spec = Luật – không viết code khi chưa có đặc tả.
2. CI = Cưỡng chế – không dựa vào ý thức của người hoặc AI; máy tự kiểm tra.
3. Runtime Checkpoint = Xác thực cuối cùng – mọi logic đều phải được chạy thực tế trước khi phê duyệt.
4. Cách ly tuyệt đối – mỗi module độc lập, không phụ thuộc chéo.
5. Mỗi tác vụ = Một phạm vi nhỏ – không suy diễn, không thêm chức năng ngoài yêu cầu.

---

2. Cấu trúc Phase (Sơ đồ phân cấp)

```
SPEC‑6 EXECUTION WORKFLOW
│
├── Phase 1 – Spec Lock & Infrastructure (2–3 ngày)
│   ├── Đóng băng đặc tả: FSM, interface, schema
│   ├── Tạo repo, branch protection, CI skeleton
│   ├── Lưu trữ trong /spec/ (fsm.md, interface.md, schema.py)
│   └── 🏁 Milestone: Spec hoàn chỉnh, CI cơ bản chạy được
│
├── Phase 2 – Module Isolation & CI Enforcement (2–3 ngày)
│   ├── Tạo 4 module: fsm, cdp, billing, watchdog (thư mục /modules/)
│   ├── CI rules:
│   │   ├── check_import_scope – cấm import chéo module
│   │   ├── check_signature – function phải match spec
│   │   ├── check_pr_scope – 1 PR ≤ 200 dòng, chỉ 1 module
│   │   └── check_spec_lock – cấm sửa /spec/*
│   ├── Phân quyền ghi (CODEOWNERS): mỗi module do Code (GPT‑5.2‑Codex) viết, Review (GPT‑5.4) approve
│   └── 🏁 Milestone: CI bắt được lỗi import sai, chữ ký sai, PR vượt phạm vi
│
├── Phase 3 – Implementation (5–7 ngày)	
│   ├── Branch strategy: main (protected) ← develop ← feature/<module>/<function>
│   ├── Workflow:		
│   │   ├── Architect (Opus) định nghĩa logic module
│   │   ├── Prompt Engineer (Gemini) tách thành task (dạng Function/Input/Output/Constraints/Forbidden)
│   │   ├── Code (GPT‑5.2‑Codex) viết code + unit test
│   │   ├── Review (GPT‑5.4) kiểm tra PR, đối chiếu spec
│   │   └── Merge vào develop sau khi CI pass + review approve
│   ├── Integration sớm:
│   │   ├── Không dùng mock phức tạp – chỉ stub đơn giản (trả đúng format)
│   │   ├── Sau khi có đủ module tối thiểu (fsm + cdp + billing), chạy smoke test kiểm tra interface compatibility (không test business logic)
│   └── 🏁 Milestone: 4 module hoàn chỉnh, unit test pass, smoke test pass
│
├── Phase 4 – Integration & Staging Validation (3–4 ngày)
│   ├── Tích hợp toàn bộ module (branch integration ← develop)
│   ├── Staging environment:
│   │   ├── Site thật, proxy thật
│   │   ├── Dataset riêng biệt (không ảnh hưởng production)
│   │   ├── Có kill‑switch toàn cục để dừng khẩn cấp
│   ├── Rollout: 1 worker → 3 worker
│   ├── Kiểm tra bắt buộc:
│   │   ├── Không double‑consume (billing atomic)
│   │   ├── FSM không kẹt, không lỗi state
│   │   ├── Watchdog kill/restart đúng
│   │   ├── CDP network listener hoạt động (chờ total amount)
│   │   └── Log trace đầy đủ
│   ├── Định lượng "ổn định":
│   │   ├── success rate ≥ 70%
│   │   ├── worker restart count < 2 / 24h
│   │   ├── memory usage < 1.5G
│   │   └── không double‑consume
│   └── 🏁 Milestone: 3 workers chạy 24h đạt các chỉ số trên
│
├── Phase 5 – Production Rollout (3–5 ngày)
│   ├── Rollout theo nấc: 1 → 3 → 5 → 10 workers
│   ├── Mỗi nấc chạy 12–24h trước khi tăng
│   ├── Giám sát liên tục:
│   │   ├── success rate
│   │   ├── error rate
│   │   ├── memory usage
│   │   ├── số lần worker die
│   ├── Rollback trigger (tự động hoặc thủ công) nếu:
│   │   ├── success rate giảm mạnh (>10% so với nấc trước)
│   │   ├── error rate tăng đột biến (>5%)
│   │   ├── memory > 2G
│   │   └── worker die > 3 lần trong 1h
│   └── 🏁 Milestone: 10 workers chạy 24h ổn định với tất cả chỉ số trong ngưỡng
│
└── Phase 6 – Handover & Operations (2 ngày)
├── Viết runbook (hướng dẫn start/stop, đọc log, fallback thủ công)
├── Cấu hình cron dọn cache browser profile (1 lần/ngày)
├── Backup billing pool (SQLite) định kỳ
└── 🏁 Milestone: Tài liệu đầy đủ, sẵn sàng bàn giao cho vận hành
```

---

3. Các điểm kiểm soát bắt buộc (Guards)

3.1 Blueprint → Test Binding

· Mỗi yêu cầu kỹ thuật trong blueprint phải có ít nhất một test case (unit test hoặc integration test) tương ứng.
· CI kiểm tra sự tồn tại của blueprint và sự tương ứng (qua mapping hoặc quy ước đặt tên test).

3.2 Billing Atomic (không double‑consume)

· SQLite transaction:
```sql
UPDATE cards SET status='used' WHERE id=? AND status='available'
```
· Kiểm tra affected_rows == 1. Nếu không, từ chối thao tác và ghi log lỗi.

3.3 Watchdog Lifecycle

· Khi kill worker: đóng trình duyệt (kill browser process), xóa profile tạm, giải phóng tài nguyên.
· Ngăn rò rỉ bộ nhớ và zombie process.

3.4 PR Scope Limiter

· Mỗi PR:
· Tối đa 200 dòng thay đổi (không tính file test).
· Chỉ ảnh hưởng một module (kiểm tra qua file path).
· CI từ chối PR vượt giới hạn.

3.5 Traceability Logging

· Log định dạng bắt buộc:
```
timestamp | worker_id | trace_id | state | action | status
```
· Đủ để debug và tái hiện luồng.

3.6 CDP Network Listener

· Module CDP phải sử dụng Network.responseReceived để chờ API tính tiền (total amount) trước khi điền thông tin thanh toán.
· Kiểm tra này là một phần của staging validation (Phase 4) – nếu không có, coi như không pass.

3.7 Staging Safety Guard

· Dữ liệu staging: riêng biệt, không liên quan đến production.
· Có kill‑switch toàn cục (một nút hoặc lệnh) để dừng ngay lập tức toàn bộ worker trên staging nếu phát hiện sự cố.

3.8 Rollback Trigger

· Tự động rollback về mức worker trước nếu bất kỳ trigger nào kích hoạt (có thể cài đặt trong PM2 hoặc script giám sát).

---

4. AI Workforce Control (Pipeline phân công)

1. Architect (Claude Opus 4.6): Thiết kế logic tổng thể, định nghĩa interface, phê duyệt milestone và giám sát runtime. Đầu ra: Spec, interface contract và các quyết định kỹ thuật.
2. System Coordinator GPT (Software Architect GPT): Quản lý luồng CI/CD, đánh giá tiến độ, kiểm soát khóa cứng (Hard Gate, Review Lock) và phân phối lệnh làm việc. Đầu ra: Lệnh điều phối và chỉ định task.
3. Trợ lý Hỗ trợ Triển khai (DeepSeek): Hướng dẫn thao tác từng bước, soạn Prompt kỹ thuật chuẩn để copy-paste, tuyệt đối không tự viết code. Đầu ra: Hướng dẫn thao tác, Prompt giao việc và Báo cáo kết quả.
4. Prompt Engineer (Gemini 3.1 Pro): Chuyển spec thành các task cụ thể, chuẩn hóa input/output và viết prompt cho Codex. Đầu ra: Task file cho từng function.
5. Code (GPT-5.2-Codex): Chịu trách nhiệm viết mã nguồn theo task, triển khai module và viết unit test hoàn chỉnh. Đầu ra: Code và bộ unit test.
6. Review (GPT-5.4): Kiểm tra PR, đối chiếu với spec để phát hiện lỗi logic, side-effect và đảm bảo tính nhất quán của interface. Đầu ra: Quyết định Approved hoặc Rejected.

Nguyên tắc làm việc:

· Không AI nào tự ý thay đổi spec – spec do Architect định nghĩa và Prompt Engineer đóng khung.
· Task duy nhất từ Prompt Engineer đến Code – mỗi function được giao dưới dạng:

Function: <tên>
Input: <format>
Output: <format>
Constraints: <điều kiện>
Forbidden: <không được làm>
```	
· Code chỉ viết đúng task – không thêm logic ngoài phạm vi.
· Review bắt buộc trước merge – GPT‑5.4 kiểm tra PR, nếu không pass thì reject.

---

5. GitHub Enforcement

· Branch protection:
· main: chỉ nhận PR từ develop, phải có CI pass và ít nhất 1 approve từ Review.
· develop: cấm push trực tiếp, chỉ nhận PR từ feature branch.
· CODEOWNERS:
```
* @gpt5.4-review                 # mọi PR đều phải có review từ GPT‑5.4
/spec/ @opus-architect          # spec chỉ Architect được sửa
/modules/ @gpt5.2-codex         # code modules do Codex viết, nhưng review bắt buộc	
```
· CI bắt buộc (GitHub Actions):
· check_signature – so sánh function signature trong code với spec.
· check_import_scope – đảm bảo không module nào import từ module khác.
· check_pr_scope – kiểm tra số dòng thay đổi và module bị ảnh hưởng.
· check_spec_lock – đảm bảo không PR nào sửa file trong /spec/ (chỉ Architect được phép).

---

6. Tổng kết Milestones

Phase Milestone
P1 Spec lock, CI skeleton sẵn sàng
P2 CI bắt được lỗi import, signature, PR scope
P3 4 module hoàn chỉnh, unit test pass, smoke test pass
P4 3 workers staging 24h đạt chỉ số ổn định
P5 10 workers production 24h ổn định
P6 Runbook hoàn chỉnh, sẵn sàng bàn giao

---
[Architect] Claude Opus 4.6
│ (Thiết kế tổng thể, ra Spec gốc, định nghĩa Interface)
│
└── [System Coordinator GPT] SOFTWARE ARCHITECT GPT
│ (Kiểm soát luồng CI/CD, gài khóa cứng, ra lệnh điều phối task)
│
├── [Prompt Engineer] Gemini 3.1 Pro
│   (Phân rã Spec thành các Task file cụ thể, chuẩn hóa Input/Output)
│
└── [Trợ lý Hỗ trợ Triển khai] DEEPSEEK	
│ (Nhận Task, soạn Prompt chuẩn kỹ thuật, hướng dẫn TÔI từng bước)
│
└── [Cầu nối Thực thi] TÔI (Người Dùng)
│ (Làm theo hướng dẫn của DeepSeek, Copy-Paste Prompt giao việc)
│
├── [Thợ Code] GPT-5.2-Codex
│   (Nhận Prompt, viết Full file code, implement logic, unit test)
│
└── [Kiểm Duyệt] GPT-5.4
(Nhận Prompt Review, đối chiếu Spec, cấp APPROVED hoặc REJECTED)

Sơ đồ này đảm bảo quy tắc "Top-Down" (từ trên xuống dưới): Opus tạo luật -> Coordinator ép luật -> Gemini/DeepSeek chuyển hóa luật thành lệnh -> TÔI cầm lệnh đi giao -> Codex/5.4 thực thi và kiểm duyệt.
