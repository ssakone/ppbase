"""Thumbnail geometry of PocketBase's ``CreateThumb`` (``ppbase.storage.thumbs``).

Expected sizes follow ``disintegration/imaging`` v1.6.2 as called by
v0.22.34 ``tools/filesystem/filesystem.go`` l.451-499 (identical in v0.40.6
l.628-698): ``Resize`` rounds half up with a 1px minimum, ``Fit`` never
upscales and truncates, ``Fill`` always returns the exact box.  The output
format is the caller's (``ppbase.storage.serving.thumb_output``).
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from ppbase.storage.thumbs import create_thumb_bytes, parse_thumb_size

RED = (220, 20, 20)
BLUE = (20, 20, 220)


def _image(size: tuple[int, int], fmt: str = "PNG", **save: object) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (30, 160, 60)).save(output, format=fmt, **save)
    return output.getvalue()


def _two_bands(size: tuple[int, int]) -> bytes:
    """Top half red, bottom half blue."""
    width, height = size
    image = Image.new("RGB", size, BLUE)
    image.paste(RED, (0, 0, width, height // 2))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _thumb(source: bytes, size: str) -> Image.Image:
    generated = create_thumb_bytes(source, size)
    assert generated is not None
    image = Image.open(BytesIO(generated))
    image.load()
    return image


@pytest.mark.parametrize(
    ("source", "size", "expected"),
    [
        # Resize with a zero side (floor(x + 0.5), at least 1px).
        ((120, 80), "0x20", (30, 20)),
        ((120, 80), "20x0", (20, 13)),
        ((13, 2), "0x1", (7, 1)),  # 6.5 rounds up (Python's round gives 6)
        ((1000, 2), "10x0", (10, 1)),  # 0.02 -> minimum 1px
        ((20, 10), "0x40", (80, 40)),  # Resize upscales
        # Fill: always the exact box, upscaling small images.
        ((120, 80), "100x100", (100, 100)),
        ((50, 30), "100x100", (100, 100)),
        ((300, 100), "30x20t", (30, 20)),
        ((40, 80), "20x20b", (20, 20)),
        # Fit: never upscales, truncates the computed side.
        ((50, 30), "30x20f", (30, 18)),
        ((64, 30), "40x40f", (40, 18)),  # 18.75 truncated (round gives 19)
        ((30, 64), "40x40f", (18, 40)),
        ((50, 30), "500x500f", (50, 30)),
        ((50, 30), "50x30f", (50, 30)),
    ],
)
def test_thumb_dimensions_follow_imaging(
    source: tuple[int, int], size: str, expected: tuple[int, int]
) -> None:
    assert _thumb(_image(source), size).size == expected


@pytest.mark.parametrize(
    "size",
    ["0x0", "abc", "100", "100x100x", "100x100\n", "x100", "-1x5", "１００x１００", ""],
)
def test_invalid_sizes_fail_like_create_thumb(size: str) -> None:
    assert parse_thumb_size(size) is None
    assert create_thumb_bytes(_image((20, 20)), size) is None


def test_not_an_image_fails() -> None:
    assert create_thumb_bytes(b"plain text, not an image", "100x100") is None


@pytest.mark.parametrize(
    ("source", "size", "color"),
    [
        # cropAndResize (both sides >= 100): crop at the anchor first.
        ((100, 200), "50x50t", RED),
        ((100, 200), "50x50b", BLUE),
        # resizeAndCrop (a side < 100): resize to cover, then crop.
        ((40, 80), "20x20t", RED),
        ((40, 80), "20x20b", BLUE),
    ],
)
def test_fill_anchors(source: tuple[int, int], size: str, color: tuple) -> None:
    thumb = _thumb(_two_bands(source), size).convert("RGB")
    width, height = thumb.size
    for point in ((1, 1), (width - 2, height - 2), (width // 2, height // 2)):
        pixel = thumb.getpixel(point)
        assert all(abs(a - b) <= 12 for a, b in zip(pixel, color)), (point, pixel)


def test_center_fill_keeps_both_bands() -> None:
    thumb = _thumb(_two_bands((100, 200)), "50x50").convert("RGB")
    top = thumb.getpixel((25, 2))
    bottom = thumb.getpixel((25, 47))
    assert top[0] > 150 and bottom[2] > 150


def test_same_size_fill_is_a_clone() -> None:
    source = _two_bands((100, 100))
    thumb = _thumb(source, "100x100").convert("RGB")
    original = Image.open(BytesIO(source)).convert("RGB")
    assert list(thumb.getdata()) == list(original.getdata())


@pytest.mark.parametrize("source_format", ["PNG", "JPEG", "GIF", "WEBP", "BMP"])
@pytest.mark.parametrize("output_format", ["PNG", "JPEG", "GIF", "TIFF", "BMP"])
def test_output_format_is_the_callers(source_format: str, output_format: str) -> None:
    """The caller picks the format (``serving.thumb_output``), not the source."""
    generated = create_thumb_bytes(
        _image((60, 40), source_format), "20x20", output_format
    )
    assert generated is not None
    with Image.open(BytesIO(generated)) as image:
        assert image.format == output_format
        assert image.size == (20, 20)


def test_unknown_output_format_fails() -> None:
    assert create_thumb_bytes(_image((60, 40)), "20x20", "WEBP") is None


def test_jpeg_exif_orientation_is_applied() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise when displayed
    source = _image((40, 20), "JPEG", exif=exif)
    # Displayed as 20x40: a 0x10 thumbnail is 5x10, not 20x10.
    assert _thumb(source, "0x10").size == (5, 10)


def test_animated_gif_keeps_the_first_frame_only() -> None:
    frames = [Image.new("RGB", (40, 40), color) for color in (RED, BLUE)]
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:])
    generated = create_thumb_bytes(output.getvalue(), "20x20", "GIF")
    assert generated is not None
    thumb = Image.open(BytesIO(generated))
    assert thumb.format == "GIF"
    assert getattr(thumb, "n_frames", 1) == 1
    pixel = thumb.convert("RGB").getpixel((10, 10))
    assert pixel[0] > 150 and pixel[2] < 100


def test_transparency_is_kept() -> None:
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    output = BytesIO()
    image.save(output, format="PNG")
    thumb = _thumb(output.getvalue(), "20x20")
    assert thumb.mode == "RGBA"
    assert thumb.getpixel((10, 10))[3] == 0
