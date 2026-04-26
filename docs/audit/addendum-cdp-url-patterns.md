# Addendum — CDP Network URL Patterns (U-05)

**Source:** `integration/orchestrator.py`

```python
_CDP_NETWORK_URL_PATTERNS = ("/checkout/total", "/api/tax", "/api/checkout")
```

| Pattern | Rationale | Required endpoint coverage |
|---|---|---|
| `/checkout/total` | substring match for `/api/checkout/total` | ✓ `/api/checkout/total` |
| `/api/tax` | exact substring for tax endpoint | ✓ `/api/tax` |
| `/api/checkout` | prefix covering checkout endpoints | ✓ (redundant) |

`/api/checkout/total` and `/api/tax` are both covered. No gaps.

The broad `cws4.0` fallback and the parallel `_CDP_NETWORK_URL_PATTERNS_PRECISE`
tuple were removed in P3-F2 cleanup after live DevTools verification confirmed
the precise pricing endpoints above are sufficient for `notify_total`.

Lock-in test: `tests/verification/test_cdp_url_patterns.py` asserts exact membership.

**Verdict: CLEARED.**
