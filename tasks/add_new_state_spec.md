Function: add_new_state

Input:
    state_name: str

Output:
    State (frozen dataclass, field: name: str)

Constraints:
    - state_name phải nằm trong ALLOWED_STATES (spec/fsm.md)
    - state_name không được trùng trong registry
    - Thread-safe (Lock)

Error:
    - Raise ValueError nếu state_name không hợp lệ
    - Raise ValueError nếu state_name đã tồn tại

Forbidden:
    - Không tạo state ngoài ALLOWED_STATES
    - Không return boolean
    - Không sửa module khác