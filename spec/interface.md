Function: add_new_state

Input:
  * state_name

Output:
  * State

Error:
  * Raise ValueError nếu state_name không nằm trong ALLOWED_STATES (xem spec/fsm.md)
  * Raise ValueError nếu state_name đã tồn tại trong registry

Notes:
  * Thread-safe (Lock)
  * ALLOWED_STATES là tập đóng, định nghĩa tại spec/fsm.md

Function: get_current_state

Input:

Output:
  * State | None

Notes:
  * Trả về state cuối cùng được thêm, hoặc None nếu registry rỗng
  * Thread-safe (Lock)

Function: transition_to

Input:
  * target_state

Output:
  * State

Error:
  * Raise ValueError nếu target_state không nằm trong ALLOWED_STATES
  * Raise ValueError nếu target_state chưa tồn tại trong registry

Notes:
  * Thread-safe (Lock)

Function: reset_states

Input:

Output:
  * None

Notes:
  * Xóa toàn bộ registry
  * Thread-safe (Lock)
