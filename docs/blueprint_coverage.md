# Blueprint Coverage Report

Generated: 2026-04-23T16:33:14+00:00

## Summary

| Metric | Value |
|--------|-------|
| Total contracts | 97 |
| Passed | 97 |
| Failed | 0 |
| Errors | 0 |
| Skipped / Pending | 0 |
| Coverage | 100% |

## Per-Section Summary

| Section | Title | Contracts | Passed | Failed |
|---------|-------|-----------|--------|--------|
| §1 | Kiến trúc lõi & Cấu hình hệ thống | 5 | 5 | 0 |
| §2 | Khởi động & Tiêm Nhân Cách | 4 | 4 | 0 |
| §3 | Xâm nhập & Cách ly Phiên | 3 | 3 | 0 |
| §4 | Mô Phỏng Sinh Học Trên Form | 4 | 4 | 0 |
| §5 | Bơm Dữ Liệu Thanh Toán | 12 | 12 | 0 |
| §6 | Gatekeeper & Xử Lý Ngoại Lệ | 16 | 16 | 0 |
| §7 | Rút Lui & Xoay Vòng | 3 | 3 | 0 |
| §8 | Phase 10 Behavior Layer | 12 | 12 | 0 |
| §9 | Anti-Detect Layer 2 Tầng | 7 | 7 | 0 |
| §10 | Day/Night Behavior Simulation | 7 | 7 | 0 |
| §12 | Billing Selection Audit Event | 6 | 6 | 0 |
| §13 | Runtime Lifecycle & Control-Plane Safety | 12 | 12 | 0 |
| §14 | Cross-Module Stabilization — Integration Lock | 5 | 5 | 0 |
| §99 | Meta — Change Policy Enforcement | 1 | 1 | 0 |

## Contract Detail

| ID | Priority | §  | Rule (truncated) | Status | Severity |
|----|----------|----|------------------|--------|----------|
| INV-DAYNIGHT-01 | MAJOR | 10 | Biological time state: DAY (06:00–21:59) vs NIGHT (22:00–05:59), derived from th… | PASS | block_merge |
| INV-DAYNIGHT-02 | MAJOR | 10 | NIGHT typing penalty: typing speed is 15–30% slower than DAY, with the scale fac… | PASS | block_merge |
| INV-DAYNIGHT-03 | MAJOR | 10 | NIGHT hesitation + typo: hesitation (thinking) delay increases 20–40% and typo r… | PASS | block_merge |
| INV-DELAY-04 | CRITICAL | 10 | Temporal modifier bounded output: `apply_temporal_modifier()` returns 0.0 immedi… | PASS | block_merge |
| INV-DAYNIGHT-04 | MAJOR | 10 | PersonaProfile expanded for Day/Night: `active_hours` (tuple), `fatigue_threshol… | PASS | block_merge |
| INV-DAYNIGHT-05 | MAJOR | 10 | Session fatigue: after `fatigue_threshold`+1 consecutive cycles, hesitation (thi… | PASS | block_merge |
| INV-DAYNIGHT-06 | CRITICAL | 10 | Day/Night safety rules: the Day/Night model respects CRITICAL_SECTION (§8.3) wit… | PASS | block_merge |
| INV-AUDIT-01 | CRITICAL | 12 | Exactly one structured audit event is emitted per successful billing.select_prof… | PASS | block_merge |
| INV-AUDIT-02 | CRITICAL | 12 | Audit event schema is strict. Required fields: event_type (literal "billing_sele… | PASS | block_merge |
| INV-AUDIT-03 | CRITICAL | 12 | profile_id is a SHA-256 hash of the string "{first_name}|{last_name}|{profile.zi… | PASS | block_merge |
| INV-AUDIT-04 | CRITICAL | 12 | Audit event emission is non-interfering: an exception thrown by the emission pat… | PASS | block_merge |
| INV-CDP-01 | CRITICAL | 12 | _sanitize_error(msg) redacts sensitive data from any error message before it is … | PASS | block_merge |
| INV-AUDIT-05 | MAJOR | 12 | selection_method is "zip_match" when the caller provides a non-empty zip_code an… | PASS | block_merge |
| INV-RUNTIME-01 | CRITICAL | 13 | Worker state transitions follow the table IDLE → IN_CYCLE → {CRITICAL_SECTION | … | PASS | block_merge |
| INV-RUNTIME-02 | CRITICAL | 13 | runtime.reset() is test-only: it sets _behavior_delay_enabled=False and clears i… | PASS | block_merge |
| INV-RUNTIME-03 | CRITICAL | 13 | stop_worker() must read the worker state and add to _stop_requests in the same l… | PASS | block_merge |
| INV-RUNTIME-04 | MAJOR | 13 | Graceful shutdown budget allocation: stop() gives 30% of the shutdown budget to … | PASS | block_merge |
| INV-RUNTIME-05 | MAJOR | 13 | When worker failures occur before _apply_scale() executes, _pending_restarts is … | PASS | block_merge |
| INV-RUNTIME-06 | MAJOR | 13 | When monitor.get_metrics() raises, the scaling loop logs a structured event with… | PASS | block_merge |
| INV-RUNTIME-07 | MAJOR | 13 | _log_event() wraps log_sink.emit() in try/except. Each emit() failure increments… | PASS | block_merge |
| INV-RUNTIME-08 | MAJOR | 13 | start_worker() proxy cleanup on thread failure: if Thread.start() raises Runtime… | PASS | block_merge |
| INV-RUNTIME-09 | MINOR | 13 | register_signal_handlers() called from a non-main thread must NOT crash. SIGTERM… | PASS | warn |
| INV-CDP-EXEC-01 | MAJOR | 13 | _cdp_orphaned_threads counter is incremented on every caller-side CDP timeout an… | PASS | block_merge |
| INV-CDP-SHUTDOWN-01 | MAJOR | 13 | CDP executor shutdown uses shutdown(wait=False) and logs both the active request… | PASS | block_merge |
| INV-CDP-02 | MAJOR | 13 | The CDP PID registry is protected by modules.cdp.main._registry_lock. force_kill… | PASS | block_merge |
| INV-INTEGRATION-01 | CRITICAL | 14 | Concurrent rollback coordination: rollout._rollback_applied is set by force_roll… | PASS | block_merge |
| INV-INTEGRATION-02 | MAJOR | 14 | Rollback circuit breaker (behavior-triggered, 3 consecutive rollbacks → pause 30… | PASS | block_merge |
| INV-INTEGRATION-03 | MAJOR | 14 | Metrics-unavailable degraded path (cross-reference of INV-RUNTIME-06): when moni… | PASS | block_merge |
| INV-INTEGRATION-04 | MAJOR | 14 | Integration chain observability: for every decision window the chain monitor.get… | PASS | block_merge |
| INV-INTEGRATION-05 | CRITICAL | 14 | Repository rule A1: no cross-module imports between `modules/*` subpackages are … | PASS | block_merge |
| INV-SCALE-01 | CRITICAL | 1 | SCALE_STEPS is derived at runtime from MAX_WORKER_COUNT (default 10, range 1–50)… | PASS | block_merge |
| INV-ARCH-01 | MAJOR | 1 | Stagger Start: WorkerPool inserts a randomised inter-launch delay drawn from `ra… | PASS | block_merge |
| INV-ARCH-02 | MAJOR | 1 | Proxy management: static SOCKS5/HTTP proxies are mapped 1-to-1 with BitBrowser p… | PASS | warn |
| INV-ARCH-03 | CRITICAL | 1 | Repository rule A1 (architectural locality): `modules/*` subpackages must NOT im… | PASS | block_merge |
| INV-ARCH-04 | MAJOR | 1 | MAX_WORKER_COUNT configuration: default is 10; invalid values (non-integer, miss… | PASS | block_merge |
| INV-PERSONA-01 | MAJOR | 2 | Seed Hành Vi: every worker is assigned a PersonaProfile deterministically derive… | PASS | block_merge |
| INV-PERSONA-02 | MAJOR | 2 | Tab Janitor: when BitBrowser launches the browser with extra tabs (ads, home pag… | PASS | block_merge |
| INV-PERSONA-03 | MAJOR | 2 | Pre-flight Geo Check: after the Tab Janitor, the single remaining tab navigates … | PASS | block_merge |
| INV-PERSONA-04 | MAJOR | 2 | BitBrowser fingerprint lifecycle: each cycle calls the BitBrowser API fresh via … | PASS | block_merge |
| INV-SESSION-01 | CRITICAL | 3 | Hard-Reset State: inside `GivexDriver.navigate_to_egift()` the `_clear_browser_s… | PASS | block_merge |
| INV-SESSION-02 | MAJOR | 3 | Cookie banner accept: the selector is exactly `#button--accept-cookies` (SEL_COO… | PASS | warn |
| INV-SESSION-03 | MAJOR | 3 | URL navigation sequence: `navigate_to_egift` must hit exactly the Blueprint-spec… | PASS | block_merge |
| INV-FORM-01 | MAJOR | 4 | Greeting Message: `_random_greeting()` picks from a non-empty list of short blue… | PASS | block_merge |
| INV-FORM-02 | MAJOR | 4 | Recipient email + confirm must match exactly: `fill_egift_form` types `task.reci… | PASS | block_merge |
| INV-FORM-03 | MAJOR | 4 | Bounding Box Click offset: click coordinates on "Add to Cart" (`SEL_ADD_TO_CART`… | PASS | block_merge |
| INV-FORM-04 | MAJOR | 4 | CDP typing with per-seed typo rate: form fields are filled through `_type_value`… | PASS | block_merge |
| INV-PAYMENT-01 | CRITICAL | 5 | Total Watchdog must enable CDP Network (Network.enable) and listen to Network.re… | PASS | block_merge |
| INV-PAYMENT-02 | CRITICAL | 5 | Billing pool profile selection: when a matching zip is found in billing_list, re… | PASS | block_merge |
| INV-PAYMENT-03 | CRITICAL | 5 | Billing profile (name, address, phone, email) is frozen for the full cycle lifet… | PASS | block_merge |
| INV-PAYMENT-04 | CRITICAL | 5 | Billing selection is per-worker: each worker_id maintains its own shuffled list … | PASS | block_merge |
| INV-PAYMENT-05 | CRITICAL | 5 | 4x4 card typing rule: the card number field (#cws_txt_ccNum, 16 digits) is typed… | PASS | block_merge |
| INV-PAYMENT-06 | MAJOR | 5 | After filling CVV (#cws_txt_ccCvv), the cursor must linger around the COMPLETE P… | PASS | block_merge |
| INV-WATCHDOG-01 | CRITICAL | 5 | Total Watchdog state is keyed by worker_id in a per-worker registry (dict[worker… | PASS | block_merge |
| INV-WATCHDOG-02 | CRITICAL | 5 | notify_total(worker_id, value) is the single public API through which CDP networ… | PASS | block_merge |
| INV-ORCHESTRATOR-03 | CRITICAL | 5 | First-notify-wins: when both the CDP network listener and the DOM fallback race … | PASS | block_merge |
| INV-ORCHESTRATOR-04 | CRITICAL | 5 | Submitted-state crash safety: the task_id must be added to the submitted set (pe… | PASS | block_merge |
| INV-REDIS-01 | CRITICAL | 5 | Redis idempotency store failure semantics: is_duplicate() returns True on Redis … | PASS | block_merge |
| INV-PAYMENT-07 | MAJOR | 5 | Guest Checkout flow: from the shopping-cart page the bot clicks BEGIN CHECKOUT (… | PASS | block_merge |
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
| INV-TEARDOWN-01 | MAJOR | 7 | End-of-cycle teardown: when a cycle ends (Success or exhausted queue) the BitBro… | PASS | block_merge |
| INV-TEARDOWN-02 | MAJOR | 7 | Worker return to OrderQueue head: after teardown the worker unregisters its driv… | PASS | block_merge |
| INV-TEARDOWN-03 | MINOR | 7 | Swap counter reset on new cycle: the per-worker swap counter — which enforces th… | PASS | warn |
| INV-BEHAVIOR-01 | CRITICAL | 8 | Behavior layer is injected via the wrapper pattern `task_fn = wrap(task_fn, pers… | PASS | block_merge |
| INV-BEHAVIOR-02 | CRITICAL | 8 | BehaviorState enum is exactly {IDLE, FILLING_FORM, PAYMENT, VBV, POST_ACTION} an… | PASS | block_merge |
| INV-DELAY-02 | CRITICAL | 8 | CRITICAL_SECTION zero delay: `DelayEngine.is_delay_permitted()` returns False wh… | PASS | block_merge |
| INV-BEHAVIOR-03 | CRITICAL | 8 | SAFE ZONE rule (§8.4): delay is only permitted at UI-interaction points (typing,… | PASS | block_merge |
| INV-BEHAVIOR-04 | CRITICAL | 8 | NO-DELAY zone (§8.5): behavior layer must NOT inject delay into Payment submit (… | PASS | block_merge |
| INV-DELAY-01 | CRITICAL | 8 | Hard timing constraints (§8.6): MAX_TYPING_DELAY=1.8s, MAX_HESITATION_DELAY=5.0s… | PASS | block_merge |
| INV-BEHAVIOR-05 | MAJOR | 8 | Delay is clamped before apply, uses non-blocking sleep (does not block the worke… | PASS | block_merge |
| INV-BEHAVIOR-06 | MAJOR | 8 | Seeded reproducibility (§8.6): `rnd = random.Random(seed)` — each worker has its… | PASS | block_merge |
| INV-DELAY-03 | CRITICAL | 8 | Wrapper try/finally cleanup (§8.1/8.7): `modules/delay/wrapper.py::_wrapped()` a… | PASS | block_merge |
| INV-BEHAVIOR-07 | CRITICAL | 8 | Non-interference (§8.7): behavior layer does not change outcome — FSM flow is un… | PASS | block_merge |
| INV-BEHAVIOR-08 | MAJOR | 8 | Phase 9 alignment (§8.8): behavior layer respects SAFE_POINT (§8.4) and CRITICAL… | PASS | block_merge |
| INV-BEHAVIOR-09 | MAJOR | 8 | Performance constraint (§8.6): behavior layer overhead ≤ 15% versus a baseline c… | PASS | block_merge |
| INV-ANTIDETECT-01 | CRITICAL | 9 | Tầng 1 proxy: static SOCKS5/HTTP proxy is mapped 1-to-1 with each BitBrowser pro… | PASS | warn |
| INV-ANTIDETECT-02 | CRITICAL | 9 | Tầng 1 CDP input: all keyboard and mouse input is dispatched via `Input.dispatch… | PASS | block_merge |
| INV-ANTIDETECT-03 | MAJOR | 9 | Tầng 1 ghost cursor: mouse movement follows a Bézier curve path with randomised … | PASS | block_merge |
| INV-ANTIDETECT-04 | MAJOR | 9 | Tầng 1 bounding box click offset: click coordinates are randomised within (x±15,… | PASS | block_merge |
| INV-ANTIDETECT-05 | MAJOR | 9 | Tầng 2 biometrics: temporal noise follows log-normal / gaussian distribution for… | PASS | block_merge |
| INV-ANTIDETECT-06 | MAJOR | 9 | Tầng 2 burst typing + hesitation: the biometric burst pattern combines fast grou… | PASS | block_merge |
| INV-ANTIDETECT-07 | MAJOR | 9 | Tầng 2 non-interference: biometric/temporal layer NEVER breaks Tầng 1 (environme… | PASS | block_merge |
| INV-META-01 | CRITICAL | 99 | Pull requests modifying any file listed in spec/audit-lock.md "CHANGE POLICY (Po… | PASS | block_merge |
