# Operations Runbook — lush-givex-worker

## 1. Prerequisites
- Python 3.11+; env vars: `BILLING_POOL_DIR` (optional), `REDIS_URL` (optional)

## 2. Start
```python
from integration import runtime, rollout_scheduler
runtime.start(task_fn=my_task, interval=10)
rollout_scheduler.start_scheduler(task_fn=my_task, interval=300)
result = runtime.verify_deployment()  # {"passed": True, "checks": {...}, "errors": []}
```

## 3. Stop
```python
rollout_scheduler.stop_scheduler(timeout=10)
runtime.stop(timeout=30)
```
Emergency: `runtime.stop(timeout=5)` — all workers check `_stop_event` at safe points.

## 4. Reading Logs
Format: `timestamp | worker_id | trace_id | state | action | status`
Filter by trace: `grep "trace_id_here" worker.log` | Errors: `grep "| error |" worker.log`
## 5. Rollout Status
```python
from integration import rollout_scheduler
rollout_scheduler.get_scheduler_status()
# running, current_step, current_workers, next_workers, rollout_complete
```

## 6. Manual Scaling Override
```python
from modules.rollout import main as rollout
rollout.force_rollback(reason="manual override")
from integration import rollout_scheduler
rollout_scheduler.advance_step()
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
1. `rollout_scheduler.stop_scheduler()`
2. `runtime.stop(timeout=30)`
3. `runtime.get_deployment_status()`
4. `runtime.reset()`
5. Restart from §2.
