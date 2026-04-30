import io
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import (
    GivexDriver,
    _sanitize_url_for_log,
    _short_url,
)
from modules.common.exceptions import PageStateError


class SanitizeUrlForLogTests(unittest.TestCase):
    def test_strips_query_and_fragment(self):
        self.assertEqual(
            _sanitize_url_for_log("https://x.com/a/b?email=foo@bar.com&t=1#frag"),
            "https://x.com/a/b",
        )

    def test_empty_returns_empty(self):
        self.assertEqual(_sanitize_url_for_log(""), "")

    def test_unparseable_returns_safe_marker(self):
        with patch("urllib.parse.urlsplit", side_effect=Exception("bad")):
            self.assertEqual(
                _sanitize_url_for_log("https://x.com/path?email=foo"),
                "<unparseable-url>",
            )


class ShortUrlTests(unittest.TestCase):
    def test_keeps_host_and_last_segment(self):
        self.assertEqual(
            _short_url(
                "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/shopping-cart.html"
            ),
            "wwws-usa2.givex.com/.../shopping-cart.html",
        )

    def test_unparseable_returns_safe_marker(self):
        with patch("urllib.parse.urlsplit", side_effect=Exception("bad")):
            self.assertEqual(_short_url("https://x.com/path?email=foo"), "<unparseable-url>")


class WaitForUrlTransitionLoggingTests(unittest.TestCase):
    def _gd(self, urls):
        drv = MagicMock()
        it = iter(urls)
        type(drv).current_url = property(lambda self: next(it))
        return GivexDriver(drv), drv

    def test_logs_each_url_transition_at_info(self):
        gd, _ = self._gd(
            [
                "https://x.com/start?email=secret@x.com",
                "https://x.com/intermediate",
                "https://x.com/intermediate",
                "https://x.com/target",
            ]
        )
        with self.assertLogs("modules.cdp.driver", level="INFO") as cm:
            gd._wait_for_url("/target", timeout=5)
        transitions = [m for m in cm.output if "transitioned to" in m]
        self.assertGreaterEqual(len(transitions), 2)
        for line in cm.output:
            self.assertNotIn("secret@x.com", line)
            self.assertNotIn("?email=", line)

    def test_failure_includes_expected_lastseen_transitions(self):
        gd, _ = self._gd(
            [
                "https://wrong.example.com/somewhere",
                "https://wrong.example.com/somewhere",
            ]
        )
        with self.assertRaises(PageStateError) as ctx:
            gd._wait_for_url("expected-fragment", timeout=0.2)
        msg = str(ctx.exception)
        self.assertIn("expected", msg)
        self.assertIn("last_seen", msg)
        self.assertIn("wrong.example.com", msg)
        self.assertIn("transitions=", msg)

    def test_empty_current_url_is_not_logged_at_info(self):
        gd, _ = self._gd(["", "", "https://x.com/target"])
        with self.assertLogs("modules.cdp.driver", level="INFO") as cm:
            gd._wait_for_url("/target", timeout=5)
        info_transitions = [m for m in cm.output if "transitioned to" in m and "INFO" in m]
        for line in info_transitions:
            self.assertNotIn("transitioned to ''", line)
            self.assertNotIn('transitioned to ""', line)


class FailureScreenshotTests(unittest.TestCase):
    def test_disabled_by_default_is_noop(self):
        drv = MagicMock()
        gd = GivexDriver(drv)
        with patch.dict(
            "os.environ", {"FAILURE_SCREENSHOT_ENABLED": "0"}, clear=False
        ), patch("pathlib.Path.write_bytes") as write_mock:
            gd._capture_failure_screenshot("any_label")
        drv.get_screenshot_as_png.assert_not_called()
        write_mock.assert_not_called()

    def test_pillow_missing_does_not_save_when_allow_raw_off(self):
        from modules.notification import screenshot_blur

        drv = MagicMock()
        drv.get_screenshot_as_png.return_value = b"raw-bytes"
        gd = GivexDriver(drv)
        with patch.dict(
            "os.environ",
            {
                "FAILURE_SCREENSHOT_ENABLED": "1",
                "FAILURE_SCREENSHOT_ALLOW_RAW": "0",
            },
        ), patch.object(
            screenshot_blur, "capture_blurred_only", return_value=None
        ), patch("pathlib.Path.write_bytes") as write_mock:
            gd._capture_failure_screenshot("test_label")
        write_mock.assert_not_called()

    def test_pillow_missing_saves_raw_only_when_allow_raw_on(self):
        from modules.notification import screenshot_blur

        drv = MagicMock()
        drv.get_screenshot_as_png.return_value = b"raw-bytes"
        gd = GivexDriver(drv)
        with patch.dict(
            "os.environ",
            {
                "FAILURE_SCREENSHOT_ENABLED": "1",
                "FAILURE_SCREENSHOT_ALLOW_RAW": "1",
            },
        ), patch.object(
            screenshot_blur, "capture_blurred_only", return_value=None
        ), patch("pathlib.Path.write_bytes") as write_mock, patch(
            "pathlib.Path.mkdir"
        ), self.assertLogs(
            "modules.cdp.driver", level="WARNING"
        ) as cm:
            gd._capture_failure_screenshot("debug_label")
        write_mock.assert_called_once_with(b"raw-bytes")
        self.assertTrue(any("PRIVACY RISK" in m for m in cm.output))

    def test_helper_never_raises_even_on_capture_error(self):
        drv = MagicMock()
        drv.get_screenshot_as_png.side_effect = RuntimeError("boom")
        gd = GivexDriver(drv)
        with patch.dict("os.environ", {"FAILURE_SCREENSHOT_ENABLED": "1"}):
            try:
                gd._capture_failure_screenshot("err_test")
            except Exception as exc:
                self.fail(f"Helper must never raise: {exc!r}")


class CaptureBlurredOnlyTests(unittest.TestCase):
    def test_returns_none_when_pillow_missing(self):
        from modules.notification.screenshot_blur import capture_blurred_only

        drv = MagicMock()
        drv.get_screenshot_as_png.return_value = b"raw"

        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("no Pillow")
            return real_import(name, *a, **kw)

        with patch("builtins.__import__", side_effect=fake_import):
            self.assertIsNone(capture_blurred_only(drv))

    def test_does_not_overlay_card_mask_label(self):
        from modules.notification import screenshot_blur

        drv = MagicMock()
        try:
            from PIL import Image  # noqa: PLC0415

            buf = io.BytesIO()
            Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, format="PNG")
            tiny_png = buf.getvalue()
        except ImportError:
            self.skipTest("Pillow not installed in this test environment")
        drv.get_screenshot_as_png.return_value = tiny_png
        with patch.object(screenshot_blur, "_render") as mock_render:
            screenshot_blur.capture_blurred_only(drv)
        mock_render.assert_not_called()

    def test_returns_none_on_screenshot_failure(self):
        from modules.notification.screenshot_blur import capture_blurred_only

        drv = MagicMock()
        drv.get_screenshot_as_png.side_effect = RuntimeError("driver dead")
        self.assertIsNone(capture_blurred_only(drv))


class SubStepLoggingTests(unittest.TestCase):
    """run_pre_card_checkout_prepare must emit INFO sub-step boundary logs."""

    def test_info_logs_for_each_substep(self):
        from modules.cdp import driver as drv_mod

        real_prepare = drv_mod.GivexDriver.run_pre_card_checkout_prepare

        instance = MagicMock(spec=GivexDriver)
        instance.preflight_geo_check.return_value = "US"
        instance.navigate_to_egift.return_value = None
        instance.fill_egift_form.return_value = None
        instance.add_to_cart_and_checkout.return_value = None
        instance.select_guest_checkout.return_value = None
        instance._geo_checked_this_cycle = False

        task = MagicMock()
        task.recipient_email = "a@b.com"
        billing = MagicMock()
        billing.email = "billing@example.com"

        with self.assertLogs("modules.cdp.driver", level="INFO") as cm:
            real_prepare(instance, task, billing)

        joined = "\n".join(cm.output)
        self.assertIn("navigate_to_egift", joined)
        self.assertIn("fill_egift_form", joined)
        self.assertIn("add_to_cart_and_checkout", joined)
        self.assertIn("select_guest_checkout", joined)

        for line in cm.output:
            self.assertNotIn("a@b.com", line)
