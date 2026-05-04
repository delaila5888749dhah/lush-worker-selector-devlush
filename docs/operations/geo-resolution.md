# Proxy geo resolution diagnostics

- Workers emit JSON-in-message `proxy_geo_resolution` before billing selection,
  using `event_type`, `trace_id`, `timestamp_utc`, presence booleans, and reason.
- `PROXY_SERVER` supplies the server-side proxy host; credentials and raw IPs are
  not logged. Detected IP appears only as the first 12 SHA-256 hex characters.
- BitBrowser profiles may use their own proxy, so `PROXY_SERVER` may differ from
  the real browser exit IP. This diagnostic does not read profile metadata.
- MaxMind GeoLite2 free tier can omit postal codes; then billing falls back to
  round-robin with reason `maxmind_zip_missing`.
- Verify by opening `whoer.net` in BitBrowser, hashing that browser-side IP, and
  comparing the first 12 hex characters with `detected_ip_hash`.
