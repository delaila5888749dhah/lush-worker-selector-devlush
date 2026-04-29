# Operations Runbook — lush-givex-worker

## 1. Prerequisites
- Python 3.11+; env vars: `BILLING_POOL_DIR` (optional), `REDIS_URL` (optional)

## 2. Start

### 2.1 System entrypoint

The canonical way to start the system is via the `app.__main__` module:

```bash
python -m app
```

`app/__main__.py` reads the `ENABLE_PRODUCTION_TASK_FN` feature flag, selects the
appropriate `task_fn` (production or no-op stub), and calls `runtime.start()` on
your behalf.  **Do not call `runtime.start()` directly with a custom `task_fn`
outside of the gatekeeper checks in `app/__main__.py`.**  Bypassing the entrypoint
skips the feature flag, the BitBrowser lifecycle guard, the CDP driver registration,
and the `add_cdp_listener` probe — all of which must run before any purchase cycle
begins.

### 2.2 Environment variables

Set the following variables before starting:

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENABLE_PRODUCTION_TASK_FN` | **yes** (production) | `""` (off) | Set to `1`, `true`, or `yes` to activate the production browser lifecycle (`make_task_fn`). Omit or set to any other value to run a no-op stub (safe for staging validation). |
| `BITBROWSER_API_KEY` | **yes** (production) | — | API key for the BitBrowser automation service. Required when `ENABLE_PRODUCTION_TASK_FN` is on. |
| `BITBROWSER_ENDPOINT` | no | `http://127.0.0.1:54345` | Base URL for the local BitBrowser API server. |
| `GIVEX_ENDPOINT` | no | — | Givex service endpoint URL. A warning is logged on startup if unset. |
| `GIVEX_EGIFT_URL` | no | `https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/` | Override the eGift page URL (P2-2). Set to a staging/sandbox URL when Givex provides one; leave unset to target production. Validated at import time against the Givex host allowlist (`modules/cdp/driver.py:_ALLOWED_GIVEX_HOSTS`); HTTPS is required. |
| `GIVEX_PAYMENT_URL` | no | `https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/guest/payment.html` | Override the payment page URL (P2-2). Set to a staging/sandbox URL when Givex provides one; leave unset to target production. Validated at import time against the Givex host allowlist; HTTPS is required. |
| `ALLOW_NON_PROD_GIVEX_HOSTS` | no | `0` | **Security flag — staging/sandbox only.** When truthy (`1`/`true`/`yes`, case-insensitive), `GIVEX_EGIFT_URL` / `GIVEX_PAYMENT_URL` may point at a host outside `_ALLOWED_GIVEX_HOSTS`; the override is accepted but a `WARNING` is logged labelling the worker as `INSECURE/DEGRADED`. Any falsy / unset value re-enables strict allowlist enforcement. **MUST NOT be enabled in production** — see §2.5 below. The HTTPS scheme is always required; this flag does not allow `http://` downgrades. |
| `PROXY_LIST_FILE` | no | — | Path to a newline-delimited proxy list file consumed by the proxy rotator. |
| `GEOIP_DB_PATH` | no | `data/GeoLite2-City.mmdb` | Path to the MaxMind GeoLite2 City database used for zip-code derivation (F-07). |
| `MAXMIND_DB_PATH` | no | — | Legacy alias of `GEOIP_DB_PATH`, accepted for spec/blueprint compatibility. If both are set, `GEOIP_DB_PATH` wins. |
| `REDIS_URL` | no | `""` | Redis connection URL used for deduplication and idempotency. Leave unset to disable Redis-backed idempotency. |
| `WORKER_COUNT` | no | `1` | Number of concurrent worker threads. Valid range: 1–50. Must be ≤ `MAX_WORKER_COUNT`. |
| `MAX_WORKER_COUNT` | no | `10` | Upper bound (cap) for rollout worker count. Valid range: 1–50. `SCALE_STEPS` in `modules/rollout/main.py` is derived from this value; the cap is always the final step and rollout never exceeds it. See `docs/canary_rollout.md` §7 and the "Scaling the worker pool" section of `README.md`. |
| `BILLING_POOL_DIR` | no | `billing_pool` | Directory containing `.txt` billing profile files. |
| `BILLING_CB_THRESHOLD` | no | `3` | Number of consecutive billing failures before the circuit breaker trips. |
| `BILLING_CB_PAUSE` | no | `120` | Seconds the circuit breaker pauses new billing after tripping. |
| `IDEMPOTENCY_STORE_PATH` | no | `.idempotency_store.json` | File path for the local idempotency store. |
| `CDP_CALL_TIMEOUT_SECONDS` | no | `10.0` | Timeout (seconds) for synchronous CDP command calls. |
| `CDP_EXECUTOR_MAX_WORKERS` | no | `8` | Thread-pool size for the CDP command executor. |
| `BITBROWSER_POOL_MODE` | no | `0` | `1` = bật pool mode (round-robin trên profile có sẵn, tránh Operation Password). `0` giữ hành vi legacy create/delete. Xem §2.4 và Blueprint §2.1. |
| `BITBROWSER_PROFILE_IDS` | khi `POOL_MODE=1` | — | CSV profile IDs (không khoảng trắng), ví dụ `abc123,def456,ghi789`. Bắt buộc khi `BITBROWSER_POOL_MODE=1`; rỗng → startup abort rõ ràng. |

### 2.4 BitBrowser Pool Mode Setup (Blueprint §2.1)

Khi `BITBROWSER_POOL_MODE=1`, bot sử dụng pool profile có sẵn thay vì
create/delete mỗi cycle (cách cũ bị Operation Password của BitBrowser chặn).

Bước vận hành:

1. Mở BitBrowser GUI → tạo thủ công `N ≥ WORKER_COUNT × 2` profile. Ghi lại
   từng profile ID.
2. Điền `.env`:
   ```
   BITBROWSER_POOL_MODE=1
   BITBROWSER_PROFILE_IDS=abc123,def456,ghi789,jkl012,mno345
   ```
3. Khởi động `python -m app`. Mỗi cycle sẽ:
   - `acquire_profile()` theo round-robin (skip profile đang BUSY),
   - POST `/browser/update/partial` random lại fingerprint,
   - POST `/browser/open` nhận metadata attach Selenium (`webdriver` legacy,
     hoặc `http` + `driver` trên BitBrowser v144/v146+),
   - Kết thúc: `/browser/close` (KHÔNG delete) + trả profile về pool.
4. Nếu API trả 404 cho 1 profile → log ERROR, evict runtime khỏi pool, tiếp tục.
5. Nếu mọi profile BUSY > 60s → `RuntimeError` (scale thêm profile hoặc
   giảm `WORKER_COUNT`).

> **BitBrowser version compatibility:** both legacy (pre-v144, response
> contains `webdriver` field) and current (v144/v146+, response contains
> `http` + `driver` fields) are supported automatically — no configuration
> change is required when upgrading BitBrowser.  Current versions attach via
> local chromedriver + `ChromeOptions.debugger_address`; the `http` DevTools
> endpoint is not used as a Selenium Remote URL.

Rollback: đặt `BITBROWSER_POOL_MODE=0` → quay về legacy create/delete flow
(hành vi không đổi, hoàn toàn backward-compatible).

### 2.4.1 BitBrowser business-error troubleshooting

If you see:

```
RuntimeError: BitBrowser API /browser/open returned business error: msg='The IP changed, stop open profile.'
```

the BitBrowser profile has IP-change protection enabled and the proxy assigned
to it is producing a different egress IP than the recorded baseline.  Either
disable IP detection on the profile, switch to a sticky-session proxy, or reset
the profile's IP baseline in the BitBrowser GUI.

Other common business errors surfaced the same way:

| `msg` | Likely cause | Remedy |
|---|---|---|
| `The IP changed, stop open profile.` | IP-change protection triggered | Disable IP detection or use sticky-session proxy |
| `proxy authentication failure` | Wrong proxy credentials | Update proxy credentials in the BitBrowser profile |
| `profile already open` | Another process has the profile open | Close the profile in BitBrowser GUI then retry |

### 2.5 Givex host allowlist (`ALLOW_NON_PROD_GIVEX_HOSTS`)

`modules/cdp/driver.py` validates `GIVEX_EGIFT_URL` and `GIVEX_PAYMENT_URL`
against `_ALLOWED_GIVEX_HOSTS` at module import time so a typo or a
malicious override cannot redirect the bot to a typo-squat / phishing
host. The `https://` scheme is always required and is **never** bypassable.

**Production (default — recommended):**

- Leave `ALLOW_NON_PROD_GIVEX_HOSTS` unset (or `0` / `false`). Only the
  hosts in `_ALLOWED_GIVEX_HOSTS` (currently `wwws-usa2.givex.com`) are
  accepted; any other host raises `RuntimeError` at import.

**Staging / sandbox only:**

- Set `ALLOW_NON_PROD_GIVEX_HOSTS=1` (or `true` / `yes`) when Givex hands
  out a non-prod URL (e.g. `https://staging.givex.com/...`). The override
  is accepted but the worker logs a `WARNING` labelled `INSECURE/DEGRADED`
  on every import — that line is intentional, treat any occurrence in a
  production log stream as a misconfiguration alert.

**Never:**

- Do **not** set `ALLOW_NON_PROD_GIVEX_HOSTS=1` in production, in shared
  prod-like environments, or in CI runs that exercise the production
  task-fn (`ENABLE_PRODUCTION_TASK_FN=1`). The flag is a deliberate
  fail-open and exists only so QA can point the bot at a sandbox host.
- Do **not** use the flag to silence an `http://` rejection — the scheme
  check is independent and is not bypassable. Fix the URL instead.

### 2.3 Verify deployment

After the process is running, confirm all checks pass:

```python
from integration import runtime
result = runtime.verify_deployment()
# {"passed": True, "checks": {...}, "errors": []}
```

## 3. Stop
```python
runtime.stop(timeout=30)
```
Emergency: `runtime.stop(timeout=5)` — all workers check `_stop_event` at safe points.

> Note: ``integration.rollout_scheduler`` is DEPRECATED (thin shim). Rollout
> lifecycle is now owned by ``integration.runtime``.

## 4. Reading Logs
Format: `timestamp | worker_id | trace_id | state | action | status`
Filter by trace: `grep "trace_id_here" worker.log` | Errors: `grep "| error |" worker.log`
## 5. Rollout Status
```python
from integration import runtime
runtime.get_deployment_status()
# Includes current_step / current_workers fields; prefer this over the
# deprecated rollout_scheduler.get_scheduler_status() shim.
```

## 6. Manual Scaling Override
```python
from modules.rollout import main as rollout
rollout.force_rollback(reason="manual override")
rollout.try_scale_up()  # replaces rollout_scheduler.advance_step()
```

## 7. Metrics & Health
```python
from modules.monitor import main as monitor
monitor.get_metrics()  # success_rate, error_rate, restarts_last_hour, memory_usage_bytes
```

## 8. Billing Pool Management
- Pool location: `billing_pool/*.txt`
- Backup: `scripts/backup_billing_pool.py`
- Browser profile cleanup: `scripts/cleanup_browser_profiles.py`

## 9. Cron Setup
```cron
0 2 * * * /path/to/venv/bin/python /path/to/scripts/cleanup_browser_profiles.py
0 3 * * * /path/to/venv/bin/python /path/to/scripts/backup_billing_pool.py
```

## 10. Fallback Manual Procedure
1. `runtime.stop(timeout=30)` (replaces deprecated `rollout_scheduler.stop_scheduler()`)
2. `runtime.get_deployment_status()`
3. `runtime.reset()`
4. Restart via `python -m app` (see §2.1).

## 11. Canary & Rollback
- Canary rollout (5 steps, final gate before full production): see
  [`docs/canary_rollout.md`](../canary_rollout.md).
- Rollback via feature flags (no code revert): see
  [`docs/rollback.md`](../rollback.md).
