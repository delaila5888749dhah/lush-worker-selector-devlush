"""Tests for Blueprint §6 Ngã rẽ 2 success-notification path.

Covers the fixes called out in the task checklist:
  - KIỂM TRA 2: orchestrator._notify_success unwraps GivexDriver → raw Selenium
                WebDriver before calling ``get_screenshot_as_png``.
  - KIỂM TRA 3: screenshot_blur applies a heavy Gaussian blur across the whole
                image (not just an overlay) so card digits are obscured.
  - KIỂM TRA 4: the bytes passed to Telegram's sendPhoto are the blurred bytes,
                not the raw screenshot.
  - KIỂM TRA 6: exceptions in any notification step are swallowed and logged.
"""
from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from modules.notification import screenshot_blur
from modules.notification.screenshot_blur import blur_and_mask, capture_and_blur


def _png_bytes_with_text(size=(200, 80)) -> bytes:
    from PIL import Image, ImageDraw  # noqa: PLC0415
    img = Image.new("RGB", size, (255, 255, 255))
    # Draw a highly-contrasted long digit string to exercise the blur path.
    ImageDraw.Draw(img).text((5, 20), "4111111111111234", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class BlurAndMaskTests(unittest.TestCase):
    """KIỂM TRA 3: blur is applied to the real image, not just an overlay."""

    def test_blur_and_mask_returns_png(self):
        out = blur_and_mask(_png_bytes_with_text(), "4111111111111234")
        self.assertIsInstance(out, (bytes, bytearray))
        # PNG magic number.
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_blur_alters_pixels_globally(self):
        from PIL import Image  # noqa: PLC0415
        raw = _png_bytes_with_text()
        blurred = blur_and_mask(raw, "4111111111111234")
        raw_img = Image.open(io.BytesIO(raw)).convert("RGB")
        blurred_img = Image.open(io.BytesIO(blurred)).convert("RGB")
        self.assertEqual(raw_img.size, blurred_img.size)
        # Compute per-pixel differences across the full image (not just the
        # overlay corner) to confirm the whole-image blur kicked in.
        diff_pixels = 0
        raw_px = raw_img.load()
        blur_px = blurred_img.load()
        w, h = raw_img.size
        overlay_x_start = w * 3 // 5  # overlay sits in the top-right region
        for y in range(0, h, 4):
            for x in range(0, overlay_x_start, 4):  # outside overlay area
                if raw_px[x, y] != blur_px[x, y]:
                    diff_pixels += 1
        self.assertGreater(
            diff_pixels, 10,
            "Blur must change pixels across the full image, not only under the overlay."
        )

    def test_empty_input_returns_none(self):
        self.assertIsNone(blur_and_mask(b"", "4111111111111234"))

    def test_corrupt_png_falls_back_to_raw(self):
        # When PIL raises on decode, helper should log a warning and return
        # the raw bytes — never propagate (Blueprint §8.7).
        raw = b"not-a-real-png"
        out = blur_and_mask(raw, "4111111111111234")
        self.assertEqual(out, raw)


class CaptureAndBlurTests(unittest.TestCase):
    """KIỂM TRA 6: screenshot failure must not raise."""

    def test_screenshot_failure_returns_none(self):
        driver = MagicMock()
        driver.get_screenshot_as_png.side_effect = RuntimeError("boom")
        self.assertIsNone(capture_and_blur(driver, "4111111111111234"))

    def test_happy_path_returns_processed_png(self):
        driver = MagicMock()
        driver.get_screenshot_as_png.return_value = _png_bytes_with_text()
        out = capture_and_blur(driver, "4111111111111234")
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"))


class OrchestratorUnwrapsGivexDriverTests(unittest.TestCase):
    """KIỂM TRA 2: orchestrator must pass raw Selenium driver to capture_and_blur."""

    def test_notify_success_unwraps_givex_driver(self):
        from integration import orchestrator  # noqa: PLC0415

        raw_selenium = MagicMock(name="selenium_webdriver")
        raw_selenium.get_screenshot_as_png.return_value = _png_bytes_with_text()

        # Simulate a GivexDriver wrapper: only the raw ._driver has
        # get_screenshot_as_png — the wrapper itself does not.
        class _FakeGivex:
            def __init__(self, inner):
                self._driver = inner

        wrapper = _FakeGivex(raw_selenium)

        task = MagicMock()
        task.primary_card.card_number = "4111111111111234"
        task.recipient_email = "a@b.c"

        with patch("integration.orchestrator.cdp._get_driver", return_value=wrapper), \
             patch("modules.notification.telegram_notifier.send_success_notification") as send:
            orchestrator._notify_success(task, "worker-1", 12.34)  # pylint: disable=protected-access

        raw_selenium.get_screenshot_as_png.assert_called_once()
        # KIỂM TRA 4: send_success_notification received blurred bytes (PNG), not None.
        send.assert_called_once()
        args = send.call_args[0]
        self.assertEqual(args[0], "worker-1")
        self.assertIs(args[1], task)
        self.assertEqual(args[2], 12.34)
        self.assertTrue(args[3].startswith(b"\x89PNG\r\n\x1a\n"))

    def test_notify_success_swallows_driver_lookup_error(self):
        """KIỂM TRA 6: if no driver is registered, we still must not raise."""
        from integration import orchestrator  # noqa: PLC0415
        task = MagicMock()
        task.primary_card.card_number = "4111111111111234"
        with patch("integration.orchestrator.cdp._get_driver",
                   side_effect=RuntimeError("no driver")), \
             patch("modules.notification.telegram_notifier.send_success_notification") as send:
            # Must not raise.
            orchestrator._notify_success(task, "worker-1", 12.34)  # pylint: disable=protected-access
        # With no driver, screenshot is skipped and the photo leg is bypassed;
        # the text-only send_success_notification is still invoked with None.
        send.assert_called_once()
        self.assertIsNone(send.call_args[0][3])


class TelegramSendPhotoReceivesBlurredBytesTests(unittest.TestCase):
    """KIỂM TRA 4: verify the multipart payload contains the blurred bytes."""

    def test_sendphoto_body_contains_blurred_bytes(self):
        from modules.notification import telegram_notifier  # noqa: PLC0415

        captured = {}

        def _fake_post(url, data, headers=None, timeout=10):  # noqa: ARG001
            captured["url"] = url
            captured["data"] = data
            return True

        blurred = blur_and_mask(_png_bytes_with_text(), "4111111111111234")
        task = MagicMock()
        task.primary_card.card_number = "4111111111111234"
        task.recipient_email = "a@b.c"

        env = {
            "TELEGRAM_ENABLED": "1",
            "TELEGRAM_BOT_TOKEN": "tkn",
            "TELEGRAM_CHAT_ID": "42",
        }
        with patch.dict("os.environ", env, clear=False), \
             patch.object(telegram_notifier, "_post", side_effect=_fake_post):
            ok = telegram_notifier.send_success_notification("w1", task, 10.0, blurred)

        self.assertTrue(ok)
        self.assertIn("/sendPhoto", captured["url"])
        self.assertIn(blurred, captured["data"])
        # Sanity: full PAN never appears in caption (which is also in data).
        self.assertNotIn(b"4111111111111234", captured["data"].replace(blurred, b""))


class BlurModuleConstantsTests(unittest.TestCase):
    def test_blur_radius_is_heavy_enough(self):
        # Guard against regressions where a light blur lets card digits
        # remain legible on the confirmation page.
        self.assertGreaterEqual(screenshot_blur._BLUR_RADIUS, 10)  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
