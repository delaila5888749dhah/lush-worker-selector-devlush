from __future__ import annotations
import logging
import time
from typing import Dict, Set

from modules.common.exceptions import CDPCommandError
from modules.common.sanitize import sanitize_error as _sanitize_error
_log = logging.getLogger(__name__)
_ADJACENT = {'a':'sqwz','b':'vghn','c':'xdfv','d':'erfcs','e':'rdsw','f':'rtgvd','g':'tyhbf','h':'yujng','i':'uojk','j':'uikmh','k':'iolmj','l':'opk','m':'nkj','n':'bhjm','o':'iplk','p':'ol','q':'wa','r':'etdf','s':'wedaz','t':'ryfg','u':'yhij','v':'cfgb','w':'qase','x':'zsdc','y':'tugi','z':'asx','0':'9','1':'2','2':'13','3':'24','4':'35','5':'46','6':'57','7':'68','8':'79','9':'80'}
_BACKSPACE, _MAX_TYPO_RATE = '\b', 0.06
_FIELD_TYPO_CAP = {"card_number": 0.02, "cvv": 0.0, "name": 0.04, "text": 0.05, "amount": 0.0}
_DOM_CODE_MAP: Dict[str, str] = {
    **{c: f"Key{c.upper()}" for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{str(d): f"Digit{d}" for d in range(10)},
    ' ': 'Space', '\b': 'Backspace', '\n': 'Enter', '\t': 'Tab',
    '-': 'Minus', '=': 'Equal', '[': 'BracketLeft', ']': 'BracketRight',
    '\\': 'Backslash', ';': 'Semicolon', "'": 'Quote', ',': 'Comma',
    '.': 'Period', '/': 'Slash', '`': 'Backquote',
    '_': 'Minus', '+': 'Equal', '{': 'BracketLeft', '}': 'BracketRight',
    '|': 'Backslash', ':': 'Semicolon', '"': 'Quote', '<': 'Comma',
    '>': 'Period', '?': 'Slash', '~': 'Backquote',
    '!': 'Digit1', '@': 'Digit2', '#': 'Digit3', '$': 'Digit4',
    '%': 'Digit5', '^': 'Digit6', '&': 'Digit7', '*': 'Digit8',
    '(': 'Digit9', ')': 'Digit0',
}
_VK_MAP: Dict[str, int] = {
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{str(d): ord(str(d)) for d in range(10)},
    '!': 49, '@': 50, '#': 51, '$': 52, '%': 53, '^': 54,
    '&': 55, '*': 56, '(': 57, ')': 48,
    '_': 189, '+': 187, '{': 219, '}': 221, '|': 220,
    ':': 186, '"': 222, '<': 188, '>': 190, '?': 191, '~': 192,
    '-': 189, '=': 187, '[': 219, ']': 221, '\\': 220,
    ';': 186, "'": 222, ',': 188, '.': 190, '/': 191, '`': 192,
    ' ': 32, '\b': 8, '\n': 13, '\t': 9,
}
_SHIFT_REQUIRED: Set[str] = set(
    '!@#$%^&*()_+{}|:"<>?~ABCDEFGHIJKLMNOPQRSTUVWXYZ'
)

def adjacent_char(c, rnd):
    n = _ADJACENT.get(c.lower(), "")
    return rnd.choice(n) if n else c

def _dispatch(drv, el, ch, strict):
    try:
        vk = _VK_MAP.get(ch, ord(ch))
        mod = 8 if ch in _SHIFT_REQUIRED else 0
        code = _DOM_CODE_MAP.get(
            ch, f"Key{ch.upper()}" if ch.isalpha() else "",
        )
        for t in ("keyDown", "keyUp"):
            drv.execute_cdp_cmd("Input.dispatchKeyEvent", {
                "type": t, "text": ch, "key": ch, "code": code,
                "windowsVirtualKeyCode": vk, "modifiers": mod,
                "isKeypad": False,
            })
        return True
    except Exception as exc:
        # Strict mode (anti-detect): never fall back to ``el.send_keys``
        # because Selenium-native key events are ``isTrusted=False`` and
        # are flaggable. Symmetric with ``bounding_box_click``'s
        # ``CDPClickError`` policy. Raise an exception that the runtime
        # treats as a flagged session (subclass of ``SessionFlaggedError``).
        if strict:
            raise CDPCommandError(
                "Input.dispatchKeyEvent",
                _sanitize_error(f"{type(exc).__name__}: {exc}"),
            ) from exc
        _log.debug("keyboard: CDP dispatch skipped, trying send_keys", exc_info=True)
    try:
        el.send_keys(ch)
        _log.warning(
            "keyboard: CDP dispatch fell back to send_keys for char %r (strict=%s)", ch, strict,
        )
        return True
    except Exception:
        (_log.warning if strict else _log.debug)(
            "keyboard: dispatch completely failed for char %r", ch,
        )
        return False


# Named-key dispatch map for ``dispatch_key`` (DOM ``code`` + Windows VK).
# Used by callers that need to send non-character keys via CDP — e.g. the
# dropdown navigator in :func:`modules.cdp.driver._cdp_select_option`.
_NAMED_KEYS: Dict[str, Dict[str, int | str]] = {
    "ArrowDown": {"code": "ArrowDown", "vk": 40},
    "ArrowUp": {"code": "ArrowUp", "vk": 38},
    "ArrowLeft": {"code": "ArrowLeft", "vk": 37},
    "ArrowRight": {"code": "ArrowRight", "vk": 39},
    "Enter": {"code": "Enter", "vk": 13},
    "Escape": {"code": "Escape", "vk": 27},
    "Tab": {"code": "Tab", "vk": 9},
    "Space": {"code": "Space", "vk": 32},
    "Backspace": {"code": "Backspace", "vk": 8},
}


def dispatch_key(driver, key_name: str) -> bool:
    """Dispatch a single named key (e.g. ``ArrowDown``, ``Enter``) via CDP.

    Emits a ``keyDown`` + ``keyUp`` ``Input.dispatchKeyEvent`` pair with
    the proper DOM ``code`` and Windows virtual-key code so the resulting
    events are ``isTrusted=True`` — matching real user input and avoiding
    the synthetic-event fingerprint that anti-fraud heuristics flag.

    Args:
        driver: Selenium WebDriver supporting ``execute_cdp_cmd``.
        key_name: The key name (e.g. ``"ArrowDown"``, ``"Enter"``).

    Returns:
        ``True`` when both ``keyDown`` and ``keyUp`` dispatched
        successfully, ``False`` on CDP failure (logged at WARNING).

    Raises:
        ValueError: if *key_name* is not a recognised named key.
    """
    spec = _NAMED_KEYS.get(key_name)
    if spec is None:
        raise ValueError(f"dispatch_key: unsupported key name {key_name!r}")
    code = spec["code"]
    vk = spec["vk"]
    try:
        for t in ("keyDown", "keyUp"):
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                "type": t,
                "key": key_name,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "modifiers": 0,
                "isKeypad": False,
            })
        return True
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning(
            "dispatch_key: CDP dispatch failed for key %r: %s", key_name, exc,
        )
        return False

def type_value(driver, element, value, rnd, *, typo_rate=0.0, delays=None,
               strict=False, field_kind="text", engine=None, clear_first=True):
    eff = min(typo_rate, _FIELD_TYPO_CAP.get(field_kind, _MAX_TYPO_RATE), _MAX_TYPO_RATE)
    res = {"typed_chars": 0, "typos_injected": 0, "corrections_made": 0, "mode": "cdp_key", "field_kind": field_kind, "eff_typo_rate": eff}
    def _sleep(d):
        if engine:
            d = engine.accumulate_delay(d) if engine.is_delay_permitted() else 0.0
        time.sleep(d)
    if clear_first:
        try: element.clear()
        except Exception: _log.debug("type_value: clear skipped", exc_info=True)
    for i, ch in enumerate(value):
        d = delays[i] if (delays and i < len(delays)) else 0.05
        if eff > 0 and rnd.random() < eff:
            w = adjacent_char(ch, rnd)
            if w != ch:
                if _dispatch(driver, element, w, strict):
                    res["typos_injected"] += 1
                _sleep(rnd.uniform(0.4, 0.6))
                if _dispatch(driver, element, _BACKSPACE, strict):
                    res["corrections_made"] += 1
        if _dispatch(driver, element, ch, strict):
            res["typed_chars"] += 1
        if d > 0: _sleep(d)
    return res
