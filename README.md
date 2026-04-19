# lush-worker-selector

Givex e-gift card worker with production-ready session init hardening
(Blueprint §1–§6).

## Quickstart

```bash
cp .env.example .env
# Fill in BITBROWSER_API_KEY, MAXMIND_LICENSE_KEY, etc.
python scripts/download_maxmind.py        # one-time GeoLite2 download
python -m app                              # stub mode (default)
ENABLE_PRODUCTION_TASK_FN=1 python -m app  # production mode
```

See `spec/blueprint.md` for the full architectural blueprint and
`.github/AI_CONTEXT.md` for the Native AI Workflow.

## Production Deployment

### Auto-update MaxMind .mmdb

The worker ships with an **in-process hot-reload thread** (D1) that
refreshes the GeoLite2 database without a restart.

- At startup, `app/__main__.py` calls `init_maxmind_reader()` and then
  `start_maxmind_auto_reload()`.
- A daemon thread `maxmind-auto-reload` wakes every
  `MAXMIND_RELOAD_INTERVAL_HOURS` (default 24). When the `.mmdb` file's
  mtime has advanced, `_atomic_swap_reader()` installs a fresh reader and
  closes the previous one after a 5s grace period so in-flight lookups
  finish safely. The swap is a single-opcode global rebinding and is
  therefore atomic with respect to concurrent `maxmind_lookup_zip` calls.
- On shutdown, an `atexit` hook calls `stop_maxmind_auto_reload()`.

**Get a license key**: register at
<https://www.maxmind.com/en/geolite2/signup> and generate a license key.

**Refresh the file on disk**:

```bash
MAXMIND_LICENSE_KEY=<your-key> python scripts/download_maxmind.py
```

#### Optional: external refresher via cron

If you prefer an OS-level schedule in addition to (or instead of) the
in-process thread, the following examples refresh the `.mmdb` file daily.

Cron (`crontab -e`):

```bash
# Refresh MaxMind GeoLite2-City.mmdb every day at 03:17 local time.
17 3 * * * cd /opt/lush-worker && \
    MAXMIND_LICENSE_KEY="$(cat /etc/lush/maxmind.key)" \
    /opt/lush-worker/.venv/bin/python scripts/download_maxmind.py \
    >> /var/log/lush/maxmind-refresh.log 2>&1
```

systemd timer — `/etc/systemd/system/lush-maxmind-refresh.service`:

```ini
[Unit]
Description=Refresh MaxMind GeoLite2 database for lush-worker-selector
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/lush/maxmind.env
WorkingDirectory=/opt/lush-worker
ExecStart=/opt/lush-worker/.venv/bin/python scripts/download_maxmind.py
User=lush
Group=lush
```

`/etc/systemd/system/lush-maxmind-refresh.timer`:

```ini
[Unit]
Description=Daily MaxMind refresh for lush-worker-selector

[Timer]
OnCalendar=*-*-* 03:17:00
RandomizedDelaySec=10min
Persistent=true
Unit=lush-maxmind-refresh.service

[Install]
WantedBy=timers.target
```

Enable with:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lush-maxmind-refresh.timer
```

The in-process hot-reloader will pick up the new file automatically on its
next tick (no process restart required).

### Billing pool sync via Google Drive Desktop

The billing pool is a flat directory of `.txt` files (format already
defined in PR #93 — one pipe-delimited record per line). You can point
`BILLING_POOL_DIR` at a Google Drive for Desktop mount to sync profiles
across machines without running `rclone` or any extra daemon.

1. Install **Google Drive for Desktop** on the production host:
   <https://www.google.com/drive/download/>.
2. Sign in and let Drive mount your account.
3. Create a folder `billing_pool/` in *My Drive* and add the `.txt`
   files there.
4. **Make the folder available offline** (right-click → *Available
   offline*) so the worker can read files even when the network blips.
5. Set `BILLING_POOL_DIR` in `.env` to the local mount path, e.g.:

   ```bash
   # Linux/Mac
   BILLING_POOL_DIR=/Volumes/GoogleDrive/My Drive/billing_pool
   # Windows
   BILLING_POOL_DIR=G:\My Drive\billing_pool
   ```

6. Restart the worker. `modules.billing.main.load_billing_pool()` will
   scan the mounted directory and reload profiles on every cycle. The
   production guard refuses any `/tmp` path (see
   `modules/billing/main.py::_is_production_mode`).

Updates made to the Drive folder on any machine propagate automatically
to all subscribed hosts — no application change required.

## Running tests

```bash
python -m pip install pytest
python -m unittest discover tests
python -m unittest discover tests/integration
```
