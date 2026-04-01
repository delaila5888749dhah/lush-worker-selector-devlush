# Interface Contract (Aggregated)

spec-version: 1.0

> **Contract Segmentation (v1.0):** Các hợp đồng giao diện đã được tách thành
> hai nhóm riêng biệt. File này tổng hợp cả hai nhóm để duy trì khả năng
> tương thích ngược với CI pipeline.
>
> - **Core (FSM):** [spec/core/interface.md](core/interface.md)
> - **Integration (Watchdog, Billing, CDP):** [spec/integration/interface.md](integration/interface.md)

## Module: fsm

Function: add_new_state
Input:
  - state_name
Output: State

Function: get_current_state
Input: None
Output: State | None

Function: transition_to
Input:
  - target_state
Output: State

Function: reset_states
Input: None
Output: None

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