# Phase 10 — Behavior Layer (Blueprint-safe)

> **Single Source of Truth:** [`spec/blueprint.md`](./blueprint.md) §8.1–§8.8 is the
> canonical specification. This document **mirrors** that content for direct
> traceability so audits can reach §10.x without having to dereference the
> Sync Matrix in `blueprint.md` §11.
>
> **Drift policy:** If this file disagrees with `blueprint.md`, **`blueprint.md`
> wins**. Update `blueprint.md` first, then re-mirror the changed sections
> here. Each sub-section links back to its §8.x counterpart.
>
> **Mapping:** §10.1 ↔ §8.1, §10.2 ↔ §8.2, …, §10.8 ↔ §8.8 (1-to-1).
> See also [`spec/blueprint.md`](./blueprint.md) §11 — Synchronization Matrix.

---

## §10.1. TÍCH HỢP THỰC THI — ARCHITECTURE (↔ [Blueprint §8.1](./blueprint.md))

· Cơ chế: Behavior được inject tại worker execution layer thông qua pattern wrapper:

    task_fn = wrap(task_fn, persona)

· Vị trí inject: Bên trong worker function, bao bọc `task_fn` gốc.

· KHÔNG can thiệp vào:
  - Runtime loop (vòng lặp điều khiển)
  - Rollout / Scaling (quản lý số lượng worker)
  - Monitor (giám sát metrics)
  - FSM (máy trạng thái — flow §6 giữ nguyên 100%)

---

## §10.2. FSM CONTEXT — BEHAVIORSTATE (↔ [Blueprint §8.2](./blueprint.md))

· Theo dõi ngữ cảnh hiện tại của worker trong cycle:
  - `IDLE` — chờ bước tiếp theo (giữa các thao tác)
  - `FILLING_FORM` — đang điền form (recipient, billing — §4)
  - `PAYMENT` — đang nhập thẻ thanh toán (card number, CVV — §5)
  - `VBV` — đang xử lý 3DS iframe (§6 Ngã rẽ 3)
  - `POST_ACTION` — chờ kết quả sau submit (§6 Gatekeeper)

· Quy tắc: Quyết định delay PHẢI dựa trên `BehaviorState` hiện tại.

---

## §10.3. CRITICAL_SECTION AWARENESS (↔ [Blueprint §8.3](./blueprint.md))

· Behavior layer KHÔNG can thiệp `CRITICAL_SECTION`:

  Các điểm `CRITICAL_SECTION` (zero delay):
  - Payment submit — click "Complete Purchase" (§5, §6)
  - VBV/3DS handling — iframe interaction + chờ loading (§6 Ngã rẽ 3)
  - API wait — CDP `Network.responseReceived` pending (§5 Watchdog)
  - Page reload operations (§6 Ngã rẽ 3, 4)

· Quy tắc: Nếu đang trong `CRITICAL_SECTION` → KHÔNG inject delay.

---

## §10.4. SAFE POINT / SAFE ZONE RULE (↔ [Blueprint §8.4](./blueprint.md))

· Nguyên tắc: Wrapper chỉ thêm delay tại các điểm an toàn (SAFE ZONE). Logic
  execution không bị thay đổi. Kết quả success/failure không bị ảnh hưởng.

· Delay CHỈ được phép tại (SAFE ZONE):
  - UI interaction (typing, click, hover)
  - Non-critical steps (form navigation, field focus)

· Delay KHÔNG được phép tại:
  - Execution control (scaling, lifecycle transitions)
  - System coordination (runtime loop, watchdog checks)

· Stagger start (§1: `random.uniform(12, 25)s`) là cơ chế RIÊNG BIỆT:
  - Stagger hoạt động giữa các worker launches
  - Behavior delay hoạt động trong cycle
  - Hai cơ chế KHÔNG can thiệp lẫn nhau

---

## §10.5. VÙNG CẤM DELAY — NO-DELAY ZONE (↔ [Blueprint §8.5](./blueprint.md))

· Behavior layer KHÔNG được inject delay vào:
  - Payment submit (Complete Purchase click event)
  - Watchdog timeout checks
  - Network wait (CDP `Network.responseReceived`)
  - VBV iframe load/interaction
  - Page reload operations

· Behavior layer KHÔNG phá watchdog:
  - Tổng delay mỗi bước ≤ 7.0s, watchdog timeout = 10s → headroom ≥ 3s
  - Delay bị clamp cứng trước khi áp dụng

· VBV 8–12s wait (§6 Ngã rẽ 3) là OPERATIONAL wait:
  - Chờ iframe loading — không phải behavioral delay
  - KHÔNG được thay thế hoặc bổ sung bởi behavior layer

---

## §10.6. KIỂM SOÁT HIỆU NĂNG & MÔ HÌNH XÁC ĐỊNH — ACTION-AWARE DELAY (↔ [Blueprint §8.6](./blueprint.md))

· Hard constraints (ràng buộc cứng):

  - `max_delay_per_action ≤ 1.8s` (typing mỗi nhóm 4 số — §4)
  - `max_delay_per_hesitation ≤ 5.0s` (thinking — §5)
  - `total_behavioral_delay_per_step ≤ 7.0s` (để lại ≥3s headroom cho watchdog 10s — §5)
  - typing và thinking loại trừ lẫn nhau trong cùng một bước cycle

· Delay phải:
  - Bị clamp (giới hạn) trước khi áp dụng — không bao giờ vượt quá max
  - Không block worker loop — delay thực hiện bằng sleep không chặn luồng chính
  - Không ảnh hưởng watchdog timeout hoặc system-level deadlines

· Overhead trung bình: ≤ 15% so với thời gian cycle không có behavior.

· Hệ thống random:

    rnd = random.Random(seed)

  Trong đó `seed` là Seed Hành Vi được cấp tại §2.

· Đảm bảo:
  - Reproducible: cùng seed → cùng pattern hành vi (tốc độ gõ, typo, hesitation)
  - Testable: có thể kiểm thử với seed cố định
  - Isolated: mỗi worker có instance `random.Random` riêng, không chia sẻ state

· Áp dụng cho:
  - Tốc độ gõ phím (typing speed distribution)
  - Tỷ lệ gõ sai (typo trigger)
  - Thời gian ngập ngừng (hesitation duration)
  - Offset click (Bounding Box ± random)

---

## §10.7. QUY TẮC KHÔNG CAN THIỆP — NON-INTERFERENCE RULE (↔ [Blueprint §8.7](./blueprint.md))

· Behavior layer KHÔNG thay đổi outcome:
  - FSM flow giữ nguyên 100% (4 ngã rẽ — §6)
  - Thứ tự bước execution không đổi
  - Kết quả success/failure không bị ảnh hưởng bởi delay
  - State transitions không bị behavior can thiệp

---

## §10.8. ĐỒNG BỘ VỚI PHASE 9 — PHASE 9 ALIGNMENT (↔ [Blueprint §8.8](./blueprint.md))

· Phase 10 PHẢI tuân thủ:
  - `SAFE_POINT` — behavior chỉ hoạt động trong ranh giới an toàn (§10.4 ↔ Blueprint §8.4)
  - `CRITICAL_SECTION` — zero can thiệp trong các thao tác quan trọng (§10.3 ↔ Blueprint §8.3)

· Phase 10 KHÔNG ĐƯỢC hoạt động ngoài phạm vi cho phép.

---

## See also

- [`spec/blueprint.md`](./blueprint.md) §8 — canonical Phase 10 specification
- [`spec/blueprint.md`](./blueprint.md) §11 — Spec ↔ Blueprint Synchronization Matrix
- [`spec/.github/SPEC-6-Native-AI-Workflow.md`](./.github/SPEC-6-Native-AI-Workflow.md) §10.1–§10.8 — workflow-level Phase 10 milestones
