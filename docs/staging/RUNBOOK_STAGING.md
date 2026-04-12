# Staging Runbook — Phase 4

> This repository does not ship a `main.py` staging CLI. The supported
> control surface is the in-process `integration.runtime` API.

## Start Staging

```bash
# 1. Verify environment
python -m unittest discover tests  # all pass

# 2. Open a Python control session and start the runtime at the default
# warm-up step (1 worker). Replace staging_task with your existing staging
# checkout callable from the deployment harness.
python
>>> from integration.runtime import get_deployment_status, start, stop
>>> from modules.rollout import main as rollout
>>> def staging_task(worker_id):
...     ...
...
>>> start(staging_task, interval=10)
True
>>> get_deployment_status()

# 3. Monitor logs during the first 5 minutes from the same process that
# started staging.

# 4. After ~30 minutes of stability, request the next rollout step (3 workers)
# from that same control session. The runtime loop will apply the target on its
# next tick.
>>> rollout.try_scale_up()
(3, 'scaled_up', [])
```

## Kill-Switch (Emergency Stop)

```bash
# In the same Python control session that started staging:
>>> stop(timeout=30)

# Out-of-band emergency stop for the staging process:
kill -SIGTERM <staging-runtime-pid>
```

## Check Metrics

```bash
# Run from the same Python control session / deployment harness process:
>>> get_deployment_status()
```

## Rollback

Automatic rollback triggers:
- error_rate > 5% → auto scale down
- restarts > 3/hr → auto scale down
- memory > 1.5GB → manual intervention needed

## Log Format

```
timestamp|worker_id|trace_id|state|action|status
```
