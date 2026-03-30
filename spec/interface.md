Function: add_new_state

Input:
  * state_name: str

Output:
  * State (frozen dataclass, field: name: str)

Error:
  * Raise ValueError nếu state_name không nằm trong ALLOWED_STATES (xem spec/fsm.md)
  * Raise ValueError nếu state_name đã tồn tại trong registry

Notes:
  * Thread-safe (Lock)
  * ALLOWED_STATES là tập đóng, định nghĩa tại spec/fsm.md

Function: add_new_state

Input:

* state_name: string
* transitions: list

Output:

* boolean (true nếu thêm thành công, false nếu state đã tồn tại)
