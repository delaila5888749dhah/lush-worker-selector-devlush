"""Session factory — create and register a GivexDriver session."""

from selenium import webdriver

from modules.cdp import main as cdp
from modules.cdp.bitbrowser import get_debugger_address
from modules.cdp.driver import GivexDriver


def create_driver_session(profile_id: str, persona=None) -> GivexDriver:
    """Launch a BitBrowser profile and return a ready ``GivexDriver``.

    Steps performed:
    1. Call :func:`~modules.cdp.bitbrowser.get_debugger_address` to open the
       browser profile and retrieve its remote debugger address.
    2. Attach Selenium to the running Chrome instance via
       ``ChromeOptions.debugger_address``.
    3. Close any extra tabs that opened with the profile.
    4. Wrap the Selenium driver in a :class:`~modules.cdp.driver.GivexDriver`.
    5. Register the driver in the CDP registry under ``profile_id``.

    Args:
        profile_id: The BitBrowser profile identifier.  Also used as the
            ``worker_id`` key in the CDP driver registry.
        persona: Optional ``PersonaProfile`` instance forwarded to
            :class:`~modules.cdp.driver.GivexDriver`.

    Returns:
        A fully initialised :class:`~modules.cdp.driver.GivexDriver` instance.
    """
    debugger_address = get_debugger_address(profile_id)

    options = webdriver.ChromeOptions()
    options.debugger_address = debugger_address

    selenium_driver = webdriver.Chrome(options=options)

    givex_driver = GivexDriver(selenium_driver, persona=persona)
    givex_driver._close_extra_tabs()

    cdp.register_driver(profile_id, givex_driver)

    return givex_driver
