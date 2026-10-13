from __future__ import annotations

import asyncio
import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app.core.config import get_settings
from app.services.style_classifier import StyleClassificationResult, StyleClassifier


def test_style_classifier_builds_bounded_metadata_free_preview(tmp_path) -> None:
    image = tmp_path / "large.png"
    Image.new("RGB", (4096, 3072), (50, 120, 80)).save(image, pnginfo=None)
    classifier = StyleClassifier.__new__(StyleClassifier)
    classifier.max_image_bytes = 8 * 1024 * 1024

    encoded, media_type = classifier._encode_image_preview(image)
    payload = base64.b64decode(encoded)
    with Image.open(io.BytesIO(payload)) as preview:
        self_size = preview.size

    assert media_type == "image/jpeg"
    assert len(payload) <= classifier.max_image_bytes
    assert max(self_size) <= 2048


def test_ingest_worker_does_not_run_visual_classification() -> None:
    source = (Path(__file__).resolve().parents[1] / "app_gui.py").read_text(encoding="utf-8")
    ingest_worker = source.split("class IngestWorker", 1)[1].split(
        "async def _classify_registered_bundle", 1
    )[0]

    assert "classify_image" not in ingest_worker


class StyleClassifierPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.classifier = StyleClassifier.__new__(StyleClassifier)
        self.classifier.max_image_bytes = 8 * 1024 * 1024

    def preview(self, image: Image.Image, **save_options) -> Image.Image:
        source = self.root / "source.png"
        image.save(source, **save_options)
        original = source.read_bytes()
        metadata = self.classifier._collect_image_metadata(source)
        encoded, media_type = self.classifier._encode_image_preview(source)
        payload = base64.b64decode(encoded)
        self.assertEqual(media_type, "image/jpeg")
        self.assertTrue(payload)
        self.assertLessEqual(len(payload), self.classifier.max_image_bytes)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.classifier._collect_image_metadata(source), metadata)
        with Image.open(io.BytesIO(payload)) as decoded:
            self.assertEqual(decoded.mode, "RGB")
            self.assertLessEqual(max(decoded.size), 2048)
            return decoded.copy()

    def test_black_transparent_graphic_has_white_background(self) -> None:
        image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        image.paste((0, 0, 0, 255), (48, 48, 80, 80))

        preview = self.preview(image)

        self.assertGreaterEqual(min(preview.getpixel((8, 8))), 245)
        self.assertLessEqual(max(preview.getpixel((64, 64))), 10)

    def test_white_transparent_graphic_has_dark_background(self) -> None:
        image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        image.paste((255, 255, 255, 255), (48, 48, 80, 80))

        preview = self.preview(image)

        self.assertLessEqual(max(preview.getpixel((8, 8))), 40)
        self.assertGreaterEqual(min(preview.getpixel((64, 64))), 245)

    def test_palette_transparency_is_composited(self) -> None:
        image = Image.new("P", (128, 128), 0)
        image.putpalette([255, 255, 255, 0, 0, 0] + [0] * 762)
        image.paste(1, (48, 48, 80, 80))

        preview = self.preview(image, transparency=0)

        self.assertGreaterEqual(min(preview.getpixel((8, 8))), 245)
        self.assertLessEqual(max(preview.getpixel((64, 64))), 10)

    def test_fully_transparent_image_has_safe_blank_preview(self) -> None:
        preview = self.preview(Image.new("RGBA", (128, 128), (200, 80, 10, 0)))

        self.assertEqual(preview.getextrema(), ((255, 255), (255, 255), (255, 255)))

    def test_transparent_preview_keeps_size_budget_and_strips_metadata(self) -> None:
        image = Image.effect_noise((4096, 3072), 100).convert("RGBA")
        image.putalpha(128)
        text = PngInfo()
        text.add_text("private_note", "source metadata")
        exif = Image.Exif()
        exif[270] = "private description"
        self.classifier.max_image_bytes = 256 * 1024

        preview = self.preview(image, pnginfo=text, exif=exif, icc_profile=b"private-profile")

        self.assertLess(max(preview.size), 2048)
        for field in ("exif", "icc_profile", "private_note", "comment", "xmp"):
            self.assertNotIn(field, preview.info)
        with Image.open(self.root / "source.png") as original:
            self.assertEqual(original.size, (4096, 3072))
            self.assertEqual(original.mode, "RGBA")
            self.assertEqual(original.info["private_note"], "source metadata")
            self.assertEqual(original.getexif()[270], "private description")

    def test_preview_profile_is_local_only_and_preserves_original(self) -> None:
        source = self.root / "large-alpha.png"
        image = Image.new("RGBA", (3000, 2000), (255, 255, 255, 128))
        text = PngInfo()
        text.add_text("private_note", "source metadata")
        exif = Image.Exif()
        exif[270] = "private description"
        image.save(source, pnginfo=text, exif=exif, icc_profile=b"private-profile")
        original = source.read_bytes()

        for transport, max_edge in (("ollama", 1024), ("openai", 2048), ("sophon", 2048)):
            with self.subTest(transport=transport):
                self.classifier.transport = transport
                encoded, media_type = self.classifier._encode_image_preview(source)
                payload = base64.b64decode(encoded)
                self.assertEqual(media_type, "image/jpeg")
                self.assertLessEqual(len(payload), self.classifier.max_image_bytes)
                with Image.open(io.BytesIO(payload)) as preview:
                    self.assertEqual(preview.mode, "RGB")
                    self.assertEqual(max(preview.size), max_edge)
                    for field in ("exif", "icc_profile", "private_note", "comment", "xmp"):
                        self.assertNotIn(field, preview.info)
                self.assertEqual(source.read_bytes(), original)


class StyleClassifierPowerTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_ollama_payload_uses_short_keep_alive(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        classifier.model = "qwen3-vl:8b"
        classifier.keep_alive = "30s"

        payload = classifier._build_request_payload("prompt", "image")

        self.assertEqual(payload["keep_alive"], "30s")

    def test_compact_request_and_prompt_are_local_only(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        classifier.model = "qwen2.5vl:3b"
        classifier.keep_alive = "30s"
        legacy_prompt = None
        for transport in ("openai", "sophon", "ollama"):
            with self.subTest(transport=transport):
                classifier.transport = transport
                prompt = classifier._build_prompt([], {"width": 3000})
                payload = classifier._build_request_payload(prompt, "image", "image/jpeg")
                self.assertEqual(payload["model"], classifier.model)
                self.assertTrue(prompt.startswith("你是 Haypile 入库视觉分拣官"))
                if legacy_prompt is None:
                    legacy_prompt = prompt
                self.assertEqual(prompt, legacy_prompt)
                if transport == "ollama":
                    self.assertEqual(payload["options"], {"temperature": 0.0, "num_ctx": 4096})
                    schema = payload["format"]
                    fields = {"role", "role_confidence", "reason", "tags", "agent_summary"}
                    self.assertEqual(set(schema["required"]), fields)
                    self.assertEqual(set(schema["properties"]), fields)
                    self.assertFalse(schema["additionalProperties"])
                    self.assertEqual(schema["properties"]["role_confidence"],
                                     {"type": "number", "minimum": 0, "maximum": 1})
                    self.assertEqual(schema["properties"]["tags"]["maxItems"], 6)
                else:
                    self.assertNotIn("format", payload)
                if transport == "openai":
                    self.assertNotIn("options", payload)
                    self.assertNotIn("keep_alive", payload)
                    self.assertEqual(payload["temperature"], 0.0)
                    self.assertEqual(payload["messages"][1]["content"][1]["image_url"]["url"],
                                     "data:image/jpeg;base64,image")
                else:
                    self.assertEqual(payload["keep_alive"], "30s")
                    if transport == "sophon":
                        self.assertEqual(payload["options"], {"temperature": 0.0})

    def test_normalize_result_keeps_ai_suggestions_conservative(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        classifier.fallback_theme = "generic"

        result = classifier._normalize_result(
            {
                "theme_id": "generic",
                "theme_confidence": 0.8,
                "role_confidence": 0.7,
                "role": "hero_image",
                "reason": "主体清晰",
                "tags": ["自然", "绿色", "绿色", "hero", "extra", "six", "seven"],
                "quality": "HIGH",
                "agent_summary": "适合作为自然风格主视觉。",
            },
            ["generic"],
        )

        self.assertEqual(result.tags, ["自然", "绿色", "hero", "extra", "six", "seven"])
        self.assertEqual(result.quality, "unknown")
        self.assertEqual(result.theme_confidence, 0.0)
        self.assertEqual(result.ai_suggestions()["usage"], "hero_image")
        self.assertEqual(result.ai_suggestions()["agent_summary"], "适合作为自然风格主视觉。")
        self.assertEqual(result.ai_suggestions()["trust"], "untrusted_advisory")
        self.assertTrue(result.ai_suggestions()["must_not_execute"])

    def test_confidence_rejects_invalid_values_and_keeps_numeric_strings(self) -> None:
        for value, expected in (
            (True, 0.0),
            (False, 0.0),
            (95, 0.0),
            (-0.1, 0.0),
            (float("nan"), 0.0),
            (float("inf"), 0.0),
            (float("-inf"), 0.0),
            (0, 0.0),
            (1, 1.0),
            (0.85, 0.85),
            ("0", 0.0),
            ("1", 1.0),
            ("0.85", 0.85),
            ("95", 0.0),
            ("-0.1", 0.0),
            ("NaN", 0.0),
            ("Infinity", 0.0),
            ("garbage", 0.0),
        ):
            with self.subTest(value=value):
                self.assertEqual(StyleClassifier._to_confidence(value), expected)

    def test_model_json_rejects_nonfinite_numbers(self) -> None:
        for payload in (
            '{"role": "logo", "role_confidence": NaN}',
            '{"role": "logo", "role_confidence": Infinity}',
            '{"role": "logo", "role_confidence": 1e309}',
            'prefix {"role": "logo", "tags": [NaN]} suffix',
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(StyleClassifier._parse_model_json(payload))

        self.assertEqual(StyleClassifier._to_confidence(float("nan")), 0.0)

    def test_prompt_is_role_only(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        prompt = classifier._build_prompt(["generic", "forest"], {"width": 800})

        self.assertIn("role_confidence", prompt)
        self.assertNotIn("theme_id", prompt)
        self.assertNotIn("候选主题", prompt)

    def test_model_call_uses_one_total_configured_timeout(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        classifier.low_power_mode = False
        classifier.enabled = True
        classifier.fallback_theme = "generic"
        classifier.transport = "ollama"
        classifier.model = "vision-model"
        classifier.keep_alive = ""
        classifier.timeout_seconds = 0.01
        classifier.confidence_threshold = 0.85
        classifier._encode_image_preview = lambda _path: ("aW1hZ2U=", "image/png")
        classifier._collect_image_metadata = lambda _path: {"width": 1, "height": 1}

        async def slow_model(_payload):
            await asyncio.sleep(0.2)
            return "", {}

        classifier._call_model = slow_model
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image.png"
            image.write_bytes(b"placeholder")
            with self.assertRaises(httpx.TimeoutException):
                asyncio.run(classifier.classify_image(image, ["generic"]))

    def test_suggestions_limit_text_and_never_publish_runtime_receipt(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        classifier.fallback_theme = "generic"
        result = classifier._normalize_result(
            {
                "role": "content_image",
                "role_confidence": 0.9,
                "reason": "r" * 200,
                "agent_summary": "s" * 200,
            },
            ["generic"],
        )
        result.runtime_receipt = {"request_body": "secret"}
        suggestions = result.ai_suggestions()

        self.assertEqual(len(suggestions["reason"]), 80)
        self.assertEqual(len(suggestions["agent_summary"]), 60)
        self.assertNotIn("runtime_receipt", suggestions)

    def test_low_power_mode_disables_model_call(self) -> None:
        with patch.dict("os.environ", {"HAYPILE_LOW_POWER_MODE": "1"}, clear=False):
            get_settings.cache_clear()
            classifier = StyleClassifier()

        result = asyncio.run(classifier.classify_image(Path("missing.png"), ["generic"]))

        self.assertFalse(classifier.enabled)
        self.assertEqual(result.reason, "low_power_mode")
        self.assertEqual(result.source, "disabled")

    def test_sophon_transport_uses_gateway_and_returns_receipt(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        classifier.transport = "sophon"
        classifier.sophon_base_url = "http://127.0.0.1:8030"
        classifier.model = "qwen2.5vl:3b"
        classifier._post_ollama_with_retry = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": " classified "}}]},
            )
        )
        receipt = {
            "schema_version": "sophon.runtime-receipt.v1",
            "request_id": "receipt-id",
            "status": "ok",
        }
        classifier._fetch_sophon_receipt = AsyncMock(return_value=receipt)

        with patch.dict("os.environ", {"ADMIN_API_KEY": "local-secret"}, clear=False):
            content, actual_receipt = asyncio.run(
                classifier._call_model({"messages": [{"images": ["aW1hZ2U="]}]})
            )

        endpoint, _ = classifier._post_ollama_with_retry.await_args.args
        headers = classifier._post_ollama_with_retry.await_args.kwargs["headers"]
        self.assertEqual(content, "classified")
        self.assertEqual(actual_receipt, receipt)
        self.assertEqual(endpoint, "http://127.0.0.1:8030/v1/chat/completions")
        self.assertEqual(headers["X-PimOS-Admin-Key"], "local-secret")
        self.assertEqual(headers["X-Sophon-Client-Id"], "haypile-vision")
        self.assertTrue(headers["X-Request-ID"].startswith("haypile-vision-"))

    def test_sophon_key_prefers_haypile_environment_name(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "key"
            key_file.write_text("haypile-secret", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "ADMIN_API_KEY": "",
                    "HAYPILE_SOPHON_API_KEY_FILE": str(key_file),
                    "PIMOS_ADMIN_API_KEY_FILE": "/missing/legacy-key",
                },
                clear=False,
            ):
                self.assertEqual(classifier._sophon_api_key(), "haypile-secret")

    def test_technical_quality_uses_role_specific_thresholds(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svg = root / "mark.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8")
            icon = root / "icon.png"
            texture = root / "texture.png"
            hero = root / "hero.png"
            small = root / "small.png"
            Image.new("RGBA", (64, 80)).save(icon)
            Image.new("RGB", (256, 300)).save(texture)
            Image.new("RGB", (800, 400)).save(hero)
            Image.new("RGB", (799, 399)).save(small)

            self.assertEqual(classifier.technical_quality(svg, "logo"), ("high", "scalable_vector"))
            self.assertEqual(classifier.technical_quality(icon, "icon")[0], "medium")
            self.assertEqual(classifier.technical_quality(texture, "texture")[0], "medium")
            self.assertEqual(classifier.technical_quality(hero, "hero_image")[0], "medium")
            self.assertEqual(classifier.technical_quality(small, "content_image")[0], "low")

    def test_auto_ready_requires_role_confidence_quality_and_known_role(self) -> None:
        accepted = StyleClassificationResult(
            theme_id="generic",
            theme_confidence=0.9,
            role_confidence=0.85,
            role="content_image",
            source="model",
            reason="clear",
            quality="medium",
        )
        self.assertTrue(StyleClassifier.is_auto_ready(accepted))
        accepted.role = "unknown"
        self.assertFalse(StyleClassifier.is_auto_ready(accepted))
        accepted.role = "content_image"
        accepted.role_confidence = 0.849
        self.assertFalse(StyleClassifier.is_auto_ready(accepted))


if __name__ == "__main__":
    unittest.main()
