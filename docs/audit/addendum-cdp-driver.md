# Addendum — CDP Driver Method Audit (U-03)

**Source read:** `modules/cdp/driver.py` — `fill_payment_and_billing`,
`submit_purchase`, `detect_page_state`, and relevant helpers.

## Navigation calls

| Method | `driver.get(...)` / `driver.navigate(...)` | Notes |
|---|---|---|
| `fill_payment_and_billing` | **NONE** | field-fill helpers only |
| `submit_purchase` | **NONE** | hesitate + `bounding_box_click` only |
| `detect_page_state` | **NONE** | reads `current_url`, no navigation |

Only `preflight_geo_check` (`self._driver.get(URL_GEO_CHECK)`) and
`navigate_to_egift` (`self._driver.get(URL_BASE)`) contain navigation; both are
outside the three audited methods.

## Selectors used

`fill_payment_and_billing`:
`SEL_CARD_NAME`, `SEL_CARD_NUMBER`, `SEL_CARD_EXPIRY_MONTH`, `SEL_CARD_EXPIRY_YEAR`,
`SEL_CARD_CVV`, `SEL_BILLING_ADDRESS`, `SEL_BILLING_COUNTRY`, `SEL_BILLING_STATE`,
`SEL_BILLING_CITY`, `SEL_BILLING_ZIP`, `SEL_BILLING_PHONE`.

`submit_purchase`: `SEL_COMPLETE_PURCHASE` (via `_hesitate_before_submit` and
`bounding_box_click`).

`detect_page_state`: `SEL_CONFIRMATION_EL`, `SEL_VBV_IFRAME`, `SEL_DECLINED_MSG`,
`SEL_UI_LOCK_SPINNER`. URL fragments: `"/confirmation"`, `"/order-confirmation"`,
`"order-confirm"` (from `URL_CONFIRM_FRAGMENTS`). Text scan: `"declined"`,
`"transaction failed"`.

All selectors match the Blueprint §4–§6 selector table exactly.  No
previously-undisclosed selectors or URLs were found.

**U-03 verdict: CLEARED** — no navigation calls in the three audited methods;
all selectors and URL strings match the blueprint.
