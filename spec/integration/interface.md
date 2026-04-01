# Interface Contract — Integration (Watchdog, Billing, CDP)

spec-version: 1.0

## Module: watchdog

Function: enable_network_monitor
Input: None
Output: None

Function: wait_for_total
Input:
  - timeout
Output: total value

## Module: billing

Function: select_profile
Input:
  - zip_code
Output: BillingProfile

## Module: cdp

Function: detect_page_state
Input: None
Output: str

Function: fill_card
Input:
  - card_info
Output: None

Function: fill_billing
Input:
  - billing_profile
Output: None

Function: clear_card_fields
Input: None
Output: None
