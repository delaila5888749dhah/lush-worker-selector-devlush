# Interface Contract — Core (FSM)

spec-version: 1.0

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
