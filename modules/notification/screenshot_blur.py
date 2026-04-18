"""Screenshot capture and card-number blur for success notifications (Blueprint §6 Ngã rẽ 2).

Captures a PNG screenshot from a Selenium driver and overlays the masked card number
in the upper-right corner.  Requires Pillow; degrades gracefully if unavailable.
"""
import io
import logging

from modules.notification.card_masker import mask_card_number

_logger = logging.getLogger(__name__)


def capture_and_blur(driver, card_number: str) -> bytes | None:
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

    try:
        from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

        img = Image.open(io.BytesIO(raw_png)).convert("RGBA")

        # Light gaussian blur for a privacy-security aesthetic
        blurred = img.filter(ImageFilter.GaussianBlur(radius=2))

        # Overlay masked card number in upper-right corner
        masked = mask_card_number(card_number)
        draw = ImageDraw.Draw(blurred)

        # Measure text size for background box
        text_padding = 6
        try:
            bbox = draw.textbbox((0, 0), masked)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        except AttributeError:
            # Older Pillow fallback
            text_w, text_h = draw.textsize(masked)  # type: ignore[attr-defined]

        img_w, _img_h = blurred.size
        margin = 10
        x0 = img_w - text_w - text_padding * 2 - margin
        y0 = margin
        x1 = img_w - margin
        y1 = y0 + text_h + text_padding * 2

        # Semi-transparent dark background
        overlay = Image.new("RGBA", blurred.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 180))
        blurred = Image.alpha_composite(blurred, overlay)

        # White text
        final_draw = ImageDraw.Draw(blurred)
        final_draw.text(
            (x0 + text_padding, y0 + text_padding),
            masked,
            fill=(255, 255, 255, 255),
        )

        out = io.BytesIO()
        blurred.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    except ImportError:
        _logger.warning(
            "capture_and_blur: Pillow not installed — returning raw screenshot. "
            "Install Pillow>=10.0.0 to enable card masking overlay."
        )
        return raw_png
    except Exception as exc:  # noqa: BLE001
        _logger.warning("capture_and_blur: image processing failed: %s", exc)
        return raw_png
