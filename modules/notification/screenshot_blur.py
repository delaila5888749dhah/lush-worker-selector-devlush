"""Screenshot capture and card-number blur for success notifications (Blueprint §6 Ngã rẽ 2).

Captures a PNG screenshot from a Selenium driver and overlays the masked card number
in the upper-right corner.  Requires Pillow; degrades gracefully if unavailable.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from modules.notification.card_masker import mask_card_number

_logger = logging.getLogger(__name__)


def _measure_text(draw, text: str):
    """Return (width, height) of ``text`` as drawn, with a Pillow-version fallback."""
    try:
        bbox = draw.textbbox((0, 0), text)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        return draw.textsize(text)  # type: ignore[attr-defined]


def _render_overlay(raw_png: bytes, masked: str) -> bytes:
    """Apply blur and overlay the masked card number on the screenshot.

    Isolated helper so :func:`capture_and_blur` keeps a small local-variable
    footprint (Pylint ``too-many-locals``).  All Pillow imports stay inside
    this function so the module remains importable when Pillow is absent.
    """
    # pylint: disable=import-outside-toplevel
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    img = Image.open(io.BytesIO(raw_png)).convert("RGBA")
    blurred = img.filter(ImageFilter.GaussianBlur(radius=2))

    draw = ImageDraw.Draw(blurred)
    text_padding = 6
    text_w, text_h = _measure_text(draw, masked)

    img_w = blurred.size[0]
    margin = 10
    x0 = img_w - text_w - text_padding * 2 - margin
    y0 = margin
    x1 = img_w - margin
    y1 = y0 + text_h + text_padding * 2

    overlay = Image.new("RGBA", blurred.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 180))
    composed = Image.alpha_composite(blurred, overlay)

    final_draw = ImageDraw.Draw(composed)
    final_draw.text(
        (x0 + text_padding, y0 + text_padding),
        masked,
        fill=(255, 255, 255, 255),
    )

    out = io.BytesIO()
    composed.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def capture_and_blur(driver, card_number: str) -> Optional[bytes]:
    """Capture a screenshot and apply a privacy blur with masked card overlay.

    Args:
        driver: Selenium WebDriver instance (or GivexDriver wrapper).
        card_number: Raw card number string to mask on the overlay.

    Returns:
        PNG bytes of the processed image, or raw screenshot bytes if Pillow is
        unavailable, or ``None`` if the screenshot itself fails.
    """
    try:
        raw_png = driver.get_screenshot_as_png()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("capture_and_blur: screenshot failed: %s", exc)
        return None

    masked = mask_card_number(card_number)
    try:
        return _render_overlay(raw_png, masked)
    except ImportError:
        _logger.warning(
            "capture_and_blur: Pillow not installed — returning raw screenshot. "
            "Install Pillow>=10.0.0 to enable card masking overlay."
        )
        return raw_png
    except Exception as exc:  # noqa: BLE001
        _logger.warning("capture_and_blur: image processing failed: %s", exc)
        return raw_png
