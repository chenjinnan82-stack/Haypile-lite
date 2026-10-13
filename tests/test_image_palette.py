from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from app.services.image_palette import dominant_colors


class ImagePaletteTests(unittest.TestCase):
    def test_colors_follow_population_and_use_uppercase_hex(self) -> None:
        with Image.new("RGB", (10, 10)) as image:
            image.putdata([(171, 205, 239)] * 60 + [(255, 0, 0)] * 30 + [(0, 255, 0)] * 10)

            self.assertEqual(dominant_colors(image), ["#ABCDEF", "#FF0000", "#00FF00"])
            self.assertEqual(dominant_colors(image, limit=1), ["#ABCDEF"])
            self.assertEqual(dominant_colors(image, limit=2), ["#ABCDEF", "#FF0000"])

    def test_limit_is_capped_and_colors_are_unique(self) -> None:
        with Image.new("RGB", (6, 1)) as image:
            image.putdata(
                [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)]
            )

            colors = dominant_colors(image, limit=100)

            self.assertEqual(len(colors), 5)
            self.assertEqual(len(colors), len(set(colors)))
            self.assertTrue(all(len(color) == 7 and color.startswith("#") for color in colors))
            self.assertEqual(dominant_colors(image, limit=0), [])
            self.assertEqual(dominant_colors(image, limit=-1), [])

    def test_invisible_and_nearly_transparent_rgb_are_excluded(self) -> None:
        with Image.new("RGBA", (100, 1)) as image:
            image.putdata([(255, 0, 0, 0)] * 60 + [(0, 0, 255, 31)] * 30 + [(12, 171, 34, 255)] * 10)

            self.assertEqual(dominant_colors(image), ["#0CAB22"])

    def test_empty_and_transparent_images_have_no_palette(self) -> None:
        for mode, size, color in (
            ("RGBA", (0, 0), (10, 20, 30, 255)),
            ("RGBA", (10, 10), (10, 20, 30, 0)),
            ("RGBA", (10, 10), (10, 20, 30, 1)),
            ("LA", (10, 10), (127, 0)),
        ):
            with self.subTest(mode=mode, size=size, color=color), Image.new(mode, size, color) as image:
                self.assertEqual(dominant_colors(image), [])

    def test_preserves_caller_image_and_metadata(self) -> None:
        with Image.new("RGBA", (256, 64), (120, 210, 15, 255)) as image:
            image.info["comment"] = "original"
            original = (image.mode, image.size, image.tobytes(), image.info.copy())

            self.assertEqual(dominant_colors(image), ["#78D20F"])

            self.assertEqual((image.mode, image.size, image.tobytes(), image.info), original)
            image.putpixel((0, 0), (1, 2, 3, 255))
            self.assertEqual(image.getpixel((0, 0)), (1, 2, 3, 255))

    def test_resizes_before_converting_and_quantizing(self) -> None:
        with Image.new("RGB", (1024, 512), (34, 51, 68)) as image:
            convert = Image.Image.convert
            quantize = Image.Image.quantize
            converted_sizes: list[tuple[int, int]] = []
            quantized_sizes: list[tuple[int, int]] = []

            def record_convert(current, *args, **kwargs):
                converted_sizes.append(current.size)
                return convert(current, *args, **kwargs)

            def record_quantize(current, *args, **kwargs):
                quantized_sizes.append(current.size)
                return quantize(current, *args, **kwargs)

            with patch.object(Image.Image, "convert", record_convert), patch.object(
                Image.Image, "quantize", record_quantize
            ):
                self.assertEqual(dominant_colors(image), ["#223344"])

            self.assertEqual(converted_sizes, [(128, 64)])
            self.assertEqual(quantized_sizes, [(128 * 64, 1)])
            self.assertEqual(image.size, (1024, 512))

    def test_common_image_modes(self) -> None:
        for mode, color, expected in (
            ("RGB", (12, 171, 34), "#0CAB22"),
            ("RGBA", (12, 171, 34, 128), "#0CAB22"),
            ("L", 128, "#808080"),
            ("LA", (128, 255), "#808080"),
            ("1", 1, "#FFFFFF"),
            ("CMYK", (0, 255, 255, 0), "#FF0000"),
        ):
            with self.subTest(mode=mode), Image.new(mode, (4, 4), color) as image:
                self.assertEqual(dominant_colors(image), [expected])

    def test_palette_mode_preserves_transparency(self) -> None:
        with Image.new("P", (10, 1)) as image:
            image.putpalette([255, 0, 0, 12, 171, 34] + [0] * (256 * 3 - 6))
            image.putdata([0] * 8 + [1] * 2)
            image.info["transparency"] = 0

            self.assertEqual(dominant_colors(image), ["#0CAB22"])


if __name__ == "__main__":
    unittest.main()
