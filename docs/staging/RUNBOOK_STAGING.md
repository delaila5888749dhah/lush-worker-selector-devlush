# Staging Runbook — Phase 4

> No `main.py` staging CLI exists in this repo; use the in-process `integration.runtime` API.

## Start / Scale / Stop

```bash
python -m unittest discover tests
python
>>> from integration.runtime import get_deployment_status, start, stop
>>> from modules.rollout import main as rollout
>>> def staging_task(worker_id): ...  # your existing staging checkout callable
>>> start(staging_task, interval=10)  # default warm-up target = 1 worker
True
>>> get_deployment_status()
>>> rollout.try_scale_up()            # after ~30 min stable, request 3 workers
(3, 'scaled_up', [])
>>> stop(timeout=30)
```

## Kill-Switch (Guard 3.7)

```bash
kill -SIGTERM <staging-runtime-pid>
```

## Check Metrics

```bash
>>> get_deployment_status()  # run in the same control session / harness process
```

## Rollback

- error_rate > 5% → auto scale down
- restarts > 3/hr → auto scale down
- memory > 1.5GB → manual intervention needed

## Log Format

```text
timestamp|worker_id|trace_id|state|action|status
```
