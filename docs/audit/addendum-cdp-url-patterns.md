# Addendum — CDP Network URL Patterns (U-05)

**Source:** `integration/orchestrator.py`

```python
_CDP_NETWORK_URL_PATTERNS = ("/checkout/total", "/api/tax", "/api/checkout", "cws4.0")
```

## Pattern inventory

| Pattern | Rationale | Covers required endpoint? |
|---|---|---|
| `"/checkout/total"` | Matches any URL containing `/checkout/total`, including `/api/checkout/total` | ✓ `/api/checkout/total` |
| `"/api/tax"` | Exact substring for the tax calculation endpoint | ✓ `/api/tax` |
| `"/api/checkout"` | Matches `/api/checkout` and `/api/checkout/total` as prefix | ✓ `/api/checkout/total` (redundant coverage) |
| `"cws4.0"` | Matches the Givex base domain path `wwws-usa2.givex.com/cws4.0/…` | ✓ all Givex endpoints |

## Coverage analysis

- **`/api/checkout/total`** — covered by both `"/checkout/total"` (substring match)
  and `"/api/checkout"` (prefix match).  **COVERED**.
- **`/api/tax`** — covered by exact match `"/api/tax"`.  **COVERED**.

No gaps identified. The four patterns provide overlapping coverage ensuring the
Total Watchdog fires on any Givex checkout/tax response.

Lock-in test: `tests/verification/test_cdp_url_patterns.py`.

**U-05 verdict: CLEARED** — all required endpoint patterns present with correct
substring coverage.
