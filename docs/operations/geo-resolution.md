# Proxy geo resolution diagnostics

Each production worker cycle that reaches geo derivation emits a
`proxy_geo_resolution` audit event before billing selection. The event is
JSON embedded in the log message and includes only PII-safe fields:
`proxy_source`, safe `proxy_host`, `detected_ip_hash`, presence booleans for
MaxMind ZIP and UTC offset, and a machine-readable `reason`.

## How `PROXY_SERVER` is used

The worker reads the `PROXY_SERVER` environment variable to identify the
server-side proxy host. Credentials in the URL are parsed out and are not
logged. The host is resolved with local DNS; the detected IP is never logged
raw and is represented as the first 12 characters of its SHA-256 hash.

## BitBrowser profile caveat

BitBrowser profiles may be configured with their own proxy settings. Those
profile-level settings can override or differ from `PROXY_SERVER`, so the
server-side proxy host used for MaxMind resolution may not be the browser's
real exit IP. This diagnostic does not read BitBrowser profile metadata.

## Why MaxMind ZIP can be missing

GeoLite2 free-tier coverage does not include postal codes for every IP range.
When MaxMind returns a city record without a postal code, billing safely falls
back to round-robin selection and the diagnostic reason is
`maxmind_zip_missing`.

## Operator verification

To compare browser-side and server-side views:

1. Open `whoer.net` inside the BitBrowser profile and note the browser exit IP.
2. Hash that IP locally with SHA-256 and compare the first 12 characters with
   the `detected_ip_hash` in the `proxy_geo_resolution` log event.
3. If the values differ, the BitBrowser profile likely uses a different proxy
   than `PROXY_SERVER`.
4. If the values match but `maxmind_zip_present=false`, MaxMind likely has no
   postal code for that IP range.
