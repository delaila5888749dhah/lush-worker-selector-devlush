Function: add_new_state

Input:
  * state_name

Output:
  * State

Error:
  * Raise ValueError nếu state_name không nằm trong ALLOWED_STATES
  * Raise ValueError nếu state_name đã tồn tại trong registry

Notes:
  * Thread-safe (Lock)
  * ALLOWED_STATES là tập đóng, định nghĩa tại spec/fsm.md

Function: get_current_state

Output:
  * State | None

Notes:
  * Thread-safe (Lock)

Function: transition_to

Input:
  * target_state

Output:
  * State

Error:
  * Raise ValueError nếu target_state không nằm trong ALLOWED_STATES
  * Raise ValueError nếu target_state chưa được đăng ký

Notes:
  * Thread-safe (Lock)

Function: reset_states

Output:
  * None

Notes:
  * Thread-safe (Lock)
  * Xoá toàn bộ registry và current_state

Function: wait_for_total

Input:
  * timeout

Output:
  * bool

Error:
  * Raise SessionFlaggedError nếu timeout

Function: enable_network_monitor

Output:
  * None

Function: select_profile

Input:
  * zip_code

Output:
  * BillingProfile

Function: detect_page_state

Output:
  * str

Function: fill_card

Input:
  * card_info

Output:
  * None

Function: fill_billing

Input:
  * billing_profile

Output:
  * None

Function: clear_card_fields

Output:
  * None
