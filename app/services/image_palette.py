"""Small, local palettes for an already decoded image preview."""

from __future__ import annotations

from PIL import Image


_SAMPLE_EDGE = 128
_MAX_COLORS = 5
_MIN_ALPHA = 32


def dominant_colors(image: Image.Image, *, limit: int = 5) -> list[str]:
    """Return up to five dominant ``#RRGGBB`` colors without changing ``image``.

    Sampling and quantization use bounded copies of the current image frame.
    Pixels with alpha below 32 are excluded so invisible RGB is not a swatch.
    """
    limit = min(_MAX_COLORS, max(0, limit))
    if not limit or not image.width or not image.height:
        return []

    scale = min(1.0, _SAMPLE_EDGE / max(image.size))
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    with image.resize(size, Image.Resampling.BILINEAR) as sample:
        with sample.convert("RGBA") as rgba:
            pixels = [
                (red, green, blue)
                for red, green, blue, alpha in rgba.get_flattened_data()
                if alpha >= _MIN_ALPHA
            ]
    if not pixels:
        return []

    with Image.new("RGB", (len(pixels), 1)) as visible:
        visible.putdata(pixels)
        with visible.quantize(
            colors=_MAX_COLORS,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        ) as quantized:
            palette = quantized.getpalette()
            counts = quantized.getcolors() or []
            ranked = [
                (count, tuple(palette[index * 3 : index * 3 + 3]))
                for count, index in counts
            ]

    colors: list[str] = []
    for _count, (red, green, blue) in sorted(ranked, key=lambda item: (-item[0], item[1])):
        color = f"#{red:02X}{green:02X}{blue:02X}"
        if color not in colors:
            colors.append(color)
        if len(colors) == limit:
            break
    return colors
