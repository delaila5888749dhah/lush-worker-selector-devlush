"""Screenshot capture + privacy blur with masked-card overlay (Blueprint §6 Ngã rẽ 2).

The blueprint requires that any card number visible on the screenshot be
obscured so the resulting PNG is safe to forward to third parties. We achieve
this by applying a heavy Gaussian blur across the entire image (rendering any
digit glyphs unreadable) and then overlaying the masked-format label
(``411111******1234``) so the recipient still has enough information to
reconcile the order.

Requires Pillow; degrades gracefully (returns raw screenshot) if absent.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from modules.notification.card_masker import mask_card_number

_logger = logging.getLogger(__name__)

# Heavy blur radius chosen so any digit-sized glyph (typical 12–18px on a
# confirmation page) is rendered unreadable even when zoomed. Keep as a module
# constant so tests and operators can reason about the safety margin.
_BLUR_RADIUS = 15


def _render(raw_png: bytes, masked: str) -> bytes:
    # pylint: disable=import-outside-toplevel
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415
    img = Image.open(io.BytesIO(raw_png)).convert("RGBA")
    # Blur the ENTIRE screenshot so any card digits that might be rendered on
    # the /confirmation page (or elsewhere) are no longer readable. This is
    # the "che kín số thẻ trên ảnh chụp màn hình thực tế" requirement from
    # Blueprint §6 Ngã rẽ 2.
    blurred = img.filter(ImageFilter.GaussianBlur(radius=_BLUR_RADIUS))
    draw = ImageDraw.Draw(blurred)
    try:
        bbox = draw.textbbox((0, 0), masked)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        tw, th = draw.textsize(masked)  # type: ignore[attr-defined]
    pad, margin = 6, 10
    x0 = blurred.size[0] - tw - pad * 2 - margin
    y0 = margin
    x1, y1 = blurred.size[0] - margin, y0 + th + pad * 2
    overlay = Image.new("RGBA", blurred.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 220))
    composed = Image.alpha_composite(blurred, overlay)
    ImageDraw.Draw(composed).text((x0 + pad, y0 + pad), masked, fill=(255, 255, 255, 255))
    out = io.BytesIO()
    composed.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def blur_and_mask(raw_png: bytes, card_number: str) -> Optional[bytes]:
    """Blur a raw PNG screenshot and overlay the masked card label.

    Returns the processed PNG bytes on success, the raw screenshot when
    Pillow is unavailable (graceful degradation), or ``None`` when inputs
    are unusable. Never raises — see Blueprint §8.7 Non-interference.
    """
    if not raw_png:
        return None
    try:
        return _render(raw_png, mask_card_number(card_number))
    except ImportError:
        _logger.warning("blur_and_mask: Pillow missing — returning raw screenshot.")
        return raw_png
    except Exception as exc:  # noqa: BLE001
        _logger.warning("blur_and_mask: image processing failed: %s", exc)
        return raw_png


def capture_and_blur(driver, card_number: str) -> Optional[bytes]:
    """Capture screenshot + blur + overlay masked card. Never raises.

    ``driver`` must be a raw Selenium ``WebDriver`` (not a wrapper); callers
    are responsible for unwrapping any higher-level object before invoking
    this helper. Returns ``None`` on capture failure so the caller can skip
    the ``sendPhoto`` leg and fall back to a text-only notification.
    """
    try:
        raw_png = driver.get_screenshot_as_png()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("capture_and_blur: screenshot failed: %s", exc)
        return None
    return blur_and_mask(raw_png, card_number)
