# Blueprint Coverage Report

Generated: 2026-04-23T12:15:49+00:00

## Summary

| Metric | Value |
|--------|-------|
| Total contracts | 16 |
| Passed | 16 |
| Failed | 0 |
| Errors | 0 |
| Skipped / Pending | 0 |
| Coverage | 100% |

## Per-Section Summary

| Section | Title | Contracts | Passed | Failed |
|---------|-------|-----------|--------|--------|
| §6 | Gatekeeper & Xử Lý Ngoại Lệ | 16 | 16 | 0 |

## Contract Detail

| ID | Priority | §  | Rule (truncated) | Status | Severity |
|----|----------|----|------------------|--------|----------|
| INV-FSM-01 | CRITICAL | 6 | ALLOWED_STATES in modules/fsm/main.py must equal set(_FSM_STATES) in integration… | PASS | block_merge |
| INV-GATEKEEPER-01 | CRITICAL | 6 | A stuck submit (click "Complete Purchase" with no loading response for 3s) must … | PASS | block_merge |
| INV-GATEKEEPER-02 | MAJOR | 6 | is_payment_page_reloaded() must use URL match against the payment page URL as it… | PASS | block_merge |
| INV-GATEKEEPER-03 | MAJOR | 6 | Confirmation detection (Ngã rẽ 2 — Success) requires a URL match on '/confirmati… | PASS | block_merge |
| INV-GATEKEEPER-04 | CRITICAL | 6 | handle_vbv_challenge() must return a string that is a valid member of ALLOWED_ST… | PASS | block_merge |
| INV-GATEKEEPER-05 | CRITICAL | 6 | 'vbv_cancelled' is in ALLOWED_STATES and is a terminal state (no outgoing FSM tr… | PASS | block_merge |
| INV-GATEKEEPER-06 | MAJOR | 6 | VBV iframe CDP click must use absolute coordinates computed as iframe_rect.left … | PASS | block_merge |
| INV-GATEKEEPER-07 | MAJOR | 6 | VBV dynamic wait must pause 8–12 seconds (random uniform) before iframe interact… | PASS | block_merge |
| INV-GATEKEEPER-08 | MAJOR | 6 | cdp_click_iframe_element() must use try/finally to call switch_to.default_conten… | PASS | block_merge |
| INV-GATEKEEPER-09 | CRITICAL | 6 | The 'declined' and 'vbv_cancelled' branches must never reload the page (Zero-Bac… | PASS | block_merge |
| INV-GATEKEEPER-10 | MAJOR | 6 | handle_something_wrong_popup() must retry the popup close 2–3 times before givin… | PASS | block_merge |
| INV-GATEKEEPER-11 | MINOR | 6 | The popup close locator must include an XPath fallback (XPATH_POPUP_CLOSE or XPA… | PASS | warn |
| INV-GATEKEEPER-12 | CRITICAL | 6 | The swap counter is bounded by OrderQueue size (len(task.order_queue)); no fixed… | PASS | block_merge |
| INV-GATEKEEPER-13 | MAJOR | 6 | TransientMonitor class exists in modules/monitor/main.py and detects a late-appe… | PASS | block_merge |
| INV-ORCHESTRATOR-02 | MAJOR | 6 | handle_outcome(state=None, ...) must log a WARNING before returning "retry". Sil… | PASS | block_merge |
| INV-GATEKEEPER-14 | MAJOR | 6 | UI-lock retry metric counters (record_ui_lock_retry, record_ui_lock_recovered, r… | PASS | block_merge |
