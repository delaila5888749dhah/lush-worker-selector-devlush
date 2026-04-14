import logging
import time
_log = logging.getLogger(__name__)
_ADJACENT = {'a':'sqwz','b':'vghn','c':'xdfv','d':'erfcs','e':'rdsw','f':'rtgvd','g':'tyhbf','h':'yujng','i':'uojk','j':'uikmh','k':'iolmj','l':'opk','m':'nkj','n':'bhjm','o':'iplk','p':'ol','q':'wa','r':'etdf','s':'wedaz','t':'ryfg','u':'yhij','v':'cfgb','w':'qase','x':'zsdc','y':'tugi','z':'asx','0':'9','1':'2','2':'13','3':'24','4':'35','5':'46','6':'57','7':'68','8':'79','9':'80'}
_BACKSPACE, _MAX_TYPO_RATE = '\b', 0.06
_FIELD_TYPO_CAP = {"card_number": 0.02, "cvv": 0.0, "name": 0.04, "text": 0.05}

def adjacent_char(c, rnd):
    n = _ADJACENT.get(c.lower(), "")
    return rnd.choice(n) if n else c

def _dispatch(drv, el, ch, strict):
    try:
        for t in ("keyDown", "keyUp"):
            drv.execute_cdp_cmd("Input.dispatchKeyEvent",
                                {"type": t, "text": ch, "key": ch, "code": "", "windowsVirtualKeyCode": ord(ch)})
        return True
    except Exception:
        _log.debug("keyboard: CDP dispatch skipped, trying send_keys", exc_info=True)
    try:
        el.send_keys(ch)
        return True
    except Exception:
        (_log.warning if strict else _log.debug)("keyboard: dispatch failed for char")
        return False

def type_value(driver, element, value, rnd, *, typo_rate=0.0, delays=None,
               strict=False, field_kind="text", engine=None):
    eff = min(typo_rate, _FIELD_TYPO_CAP.get(field_kind, _MAX_TYPO_RATE), _MAX_TYPO_RATE)
    res = {"typed_chars": 0, "typos_injected": 0, "corrections_made": 0, "mode": "cdp_key", "field_kind": field_kind, "eff_typo_rate": eff}
    def _sleep(d):
        time.sleep(engine.accumulate_delay(d) if (engine and engine.is_delay_permitted()) else (d if not engine else 0.0))
    try: element.clear()
    except Exception: _log.debug("type_value: clear skipped", exc_info=True)
    for i, ch in enumerate(value):
        d = delays[i] if (delays and i < len(delays)) else 0.05
        if eff > 0 and rnd.random() < eff:
            w = adjacent_char(ch, rnd)
            if w != ch:
                if _dispatch(driver, element, w, strict):
                    res["typos_injected"] += 1
                _sleep(max(0.08, d * 1.5))
                if _dispatch(driver, element, _BACKSPACE, strict):
                    res["corrections_made"] += 1
        if _dispatch(driver, element, ch, strict):
            res["typed_chars"] += 1
        if d > 0: _sleep(d)
    return res
