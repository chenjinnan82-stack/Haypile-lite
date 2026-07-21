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


class StyleClassifierPowerTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_ollama_payload_uses_short_keep_alive(self) -> None:
        classifier = StyleClassifier.__new__(StyleClassifier)
        classifier.model = "qwen3-vl:8b"
        classifier.keep_alive = "30s"

        payload = classifier._build_request_payload("prompt", "image")

        self.assertEqual(payload["keep_alive"], "30s")

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
        self.assertEqual(result.ai_suggestions()["usage"], "hero_image")
        self.assertEqual(result.ai_suggestions()["agent_summary"], "适合作为自然风格主视觉。")

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
