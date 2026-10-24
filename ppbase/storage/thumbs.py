"""Thumbnail generation following PocketBase's ``CreateThumb``.

The geometry is that of v0.22.34 ``tools/filesystem/filesystem.go``
l.439-526, identical in v0.40.6 l.608-738 (both use
``disintegration/imaging`` v1.6.2, ``go.mod``):

- ``0xH`` / ``Wx0``: ``imaging.Resize`` keeping the aspect ratio;
- ``WxH`` / ``WxHt`` / ``WxHb``: ``imaging.Fill`` anchored at the center, the
  top or the bottom (always exactly ``WxH``, upscaling a small image);
- ``WxHf``: ``imaging.Fit`` (never upscales, truncates the computed side);
- the ``imaging.Linear`` filter (Pillow's antialiased ``BILINEAR``), the EXIF
  orientation of a JPEG applied (``imaging.AutoOrientation(true)``), only the
  first frame of an animation, JPEG quality 95 (imaging's default).

Pillow and Go do not produce the same bytes: the dimensions, the format and
the anchoring are the contract, not the pixel values.
"""

from __future__ import annotations

import math
import re
from io import BytesIO
from typing import Any

#: v0.22.34 ``filesystem.go`` l.439 == v0.40.6 l.608 (Go's ``\d`` is ASCII).
THUMB_SIZE_RE = re.compile(r"([0-9]+)x([0-9]+)(t|b|f)?")

_JPEG_QUALITY = 95
# PIL format of the decoded original -> format of the thumbnail.  A format
# outside this table is encoded as PNG, like imaging's fallback.
_OUTPUT_FORMATS = {
    "JPEG": "JPEG",
    "MPO": "JPEG",
    "PNG": "PNG",
    "GIF": "GIF",
    "WEBP": "WEBP",
}


def parse_thumb_size(thumb_size: str) -> tuple[int, int, str] | None:
    """Return ``(width, height, mode)`` or ``None`` for an invalid size."""
    match = THUMB_SIZE_RE.fullmatch(thumb_size)
    if match is None:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width == 0 and height == 0:
        return None
    return width, height, match.group(3) or ""


def create_thumb_bytes(source: bytes, thumb_size: str) -> bytes | None:
    """Return the encoded thumbnail, or ``None`` when PocketBase would fail.

    ``None`` makes the caller serve the original, like the fallback of
    ``apis/file.go`` (v0.22.34 l.179-190, v0.40.6 l.181-193).
    """
    parsed = parse_thumb_size(thumb_size)
    if parsed is None:
        return None
    width, height, mode = parsed
    try:
        from PIL import Image, ImageOps
    except Exception:  # noqa: BLE001  # pragma: no cover - declared dependency
        return None

    try:
        with Image.open(BytesIO(source)) as opened:
            opened.load()
            source_format = (opened.format or "").upper()
            image = opened
            if source_format in {"JPEG", "MPO"}:
                image = ImageOps.exif_transpose(opened)
            image = image.convert("RGBA" if _has_alpha(image) else "RGB")
            if width == 0 or height == 0:
                thumb = _resize(image, width, height)
            elif mode == "f":
                thumb = _fit(image, width, height)
            else:
                thumb = _fill(image, width, height, mode)
            if thumb is None:
                return None
            output_format = _OUTPUT_FORMATS.get(source_format, "PNG")
            save_kwargs: dict[str, Any] = {}
            if output_format == "JPEG":
                if thumb.mode != "RGB":
                    thumb = thumb.convert("RGB")
                save_kwargs["quality"] = _JPEG_QUALITY
            output = BytesIO()
            thumb.save(output, format=output_format, **save_kwargs)
            return output.getvalue()
    except Exception:  # noqa: BLE001 - every failure falls back
        # Decoding, resizing and encoding errors (including Pillow's
        # decompression-bomb guard) all fall back to the original.
        return None


def _has_alpha(image: Any) -> bool:
    if "A" in image.getbands():
        return True
    has_transparency = getattr(image, "has_transparency_data", None)
    if isinstance(has_transparency, bool):
        return has_transparency
    return "transparency" in image.info


def _resample(image_module: Any) -> Any:
    resampling = getattr(image_module, "Resampling", None)
    if resampling is not None:
        return resampling.BILINEAR
    return image_module.BILINEAR  # pragma: no cover - Pillow < 9.1


def _resize(image: Any, width: int, height: int) -> Any | None:
    """``imaging.Resize``: a zero side keeps the ratio, rounded half up."""
    from PIL import Image

    src_w, src_h = image.size
    if width < 0 or height < 0 or (width == 0 and height == 0):
        return None
    if src_w <= 0 or src_h <= 0:
        return None
    if width == 0:
        width = int(max(1.0, math.floor(height * src_w / src_h + 0.5)))
    if height == 0:
        height = int(max(1.0, math.floor(width * src_h / src_w + 0.5)))
    if (width, height) == (src_w, src_h):
        return image.copy()
    return image.resize((width, height), _resample(Image))


def _fit(image: Any, max_w: int, max_h: int) -> Any | None:
    """``imaging.Fit``: scale down into the box, never up."""
    src_w, src_h = image.size
    if max_w <= 0 or max_h <= 0 or src_w <= 0 or src_h <= 0:
        return None
    if src_w <= max_w and src_h <= max_h:
        return image.copy()
    src_ratio = src_w / src_h
    max_ratio = max_w / max_h
    if src_ratio > max_ratio:
        new_w = max_w
        new_h = int(new_w / src_ratio)
    else:
        new_h = max_h
        new_w = int(new_h * src_ratio)
    return _resize(image, new_w, new_h)


def _fill(image: Any, width: int, height: int, mode: str) -> Any | None:
    """``imaging.Fill`` with the anchor of ``CreateThumb``."""
    anchor = {"t": "top", "b": "bottom"}.get(mode, "center")
    src_w, src_h = image.size
    if width <= 0 or height <= 0 or src_w <= 0 or src_h <= 0:
        return None
    if (src_w, src_h) == (width, height):
        return image.copy()
    src_ratio = src_w / src_h
    dst_ratio = width / height
    if src_w >= 100 and src_h >= 100:
        # cropAndResize: crop to the target ratio first, then resize.
        if src_ratio < dst_ratio:
            crop_h = src_w * height / width
            cropped = _crop_anchor(image, src_w, int(max(1.0, crop_h) + 0.5), anchor)
        else:
            crop_w = src_h * width / height
            cropped = _crop_anchor(image, int(max(1.0, crop_w) + 0.5), src_h, anchor)
        return _resize(cropped, width, height)
    # resizeAndCrop: cover the box first, then crop.
    if src_ratio < dst_ratio:
        resized = _resize(image, width, 0)
    else:
        resized = _resize(image, 0, height)
    if resized is None:
        return None
    return _crop_anchor(resized, width, height, anchor)


def _go_half(value: int) -> int:
    """Go integer division by two (truncates toward zero)."""
    return int(value / 2)


def _crop_anchor(image: Any, width: int, height: int, anchor: str) -> Any:
    """``imaging.CropAnchor``: the box at the anchor, clipped to the image."""
    src_w, src_h = image.size
    x = _go_half(src_w - width)
    if anchor == "top":
        y = 0
    elif anchor == "bottom":
        y = src_h - height
    else:
        y = _go_half(src_h - height)
    left = max(0, x)
    top = max(0, y)
    right = min(src_w, x + width)
    bottom = min(src_h, y + height)
    return image.crop((left, top, right, bottom))
