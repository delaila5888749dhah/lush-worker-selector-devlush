# Addendum — Blueprint Reconciliation (U-04)

**Source read:** `spec/blueprint.md` §6–§11 (VBV/3DS, declined swap, success,
retry cap, behavior layer, anti-detect, day/night, sync matrix).

## §6 — Gatekeeper & Exception Handling

| Blueprint item | Code status |
|---|---|
| Ngã rẽ 1 — UI-lock focus-shift retry | `detect_page_state` returns `"ui_lock"`; FSM retry wired in orchestrator — **PRESENT** |
| Ngã rẽ 2 — Success `/confirmation` detection | `URL_CONFIRM_FRAGMENTS`, `SEL_CONFIRMATION_EL` — **PRESENT** |
| Ngã rẽ 3 — VBV/3DS iframe detection | `SEL_VBV_IFRAME` detected; orchestrator returns `"await_3ds"` — **PRESENT** |
| Ngã rẽ 3 — Dynamic 8–12 s iframe wait | **NOT FOUND** in driver.py or orchestrator — spawning follow-up |
| Ngã rẽ 3 — CDP iframe absolute-coord cancel click | `SEL_VBV_CANCEL_BTN` defined but no `handle_vbv_*` method exists — spawning follow-up |
| Ngã rẽ 4 — Zero-backtrack Ctrl+A/Backspace clear | `clear_card_fields()` exists; Ctrl+A CDP event **NOT found** — spawning follow-up |
| Ngã rẽ 4 — Swap from OrderQueue | orchestrator `retry_new_card` action — **PRESENT** |
| Retry cap = OrderQueue size (no fixed number) | controlled by caller task queue — **PRESENT** |

## §7 — End-of-cycle cleanup

Cookie/storage clear (`_clear_browser_state`), BitBrowser profile return — not yet
wired in runtime.py (F-01 scope); noted.

## §8–§11 — Behavior / Anti-detect / Day-Night / Sync Matrix

Fully implemented per Phase 10 PRs; sync matrix in §11 shows ✓ ĐỒNG BỘ for all
10 spec items.  No gaps identified.

## Spawned follow-up issue titles

1. **"[follow-up U-04] Implement VBV/3DS 8–12 s dynamic wait in iframe handler"**
2. **"[follow-up U-04] Implement CDP iframe absolute-coordinate cancel-click for VBV/3DS (Blueprint §6 Ngã rẽ 3)"**
3. **"[follow-up U-04] Implement Ctrl+A + Backspace CDP clear in card-swap flow (Blueprint §6 Ngã rẽ 4)"**

**U-04 verdict: REMAINS_OPEN** — three §6 implementation gaps found; documented
above. Do not fix in this PR.
