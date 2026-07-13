from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.core.config import get_settings
from app.services.style_classifier import StyleClassifier


def test_style_classifier_rejects_large_image_before_base64(tmp_path) -> None:
    image = tmp_path / "large.png"
    image.write_bytes(b"x" * 32)
    classifier = StyleClassifier.__new__(StyleClassifier)
    classifier.enabled = True
    classifier.max_image_bytes = 8
    classifier.fallback_theme = "generic"

    result = asyncio.run(classifier.classify_image(image, candidate_themes=["generic"]))

    assert result.theme_id == "generic"
    assert result.reason == "image_too_large"
    assert result.source == "guard"


def test_ingest_worker_timeout_queues_retry_without_fallback_write() -> None:
    source = (Path(__file__).resolve().parents[1] / "app_gui.py").read_text(encoding="utf-8")
    timeout_block = source.split("except (ResourceExhaustedError, httpx.TimeoutException)", 1)[1].split(
        "except (ValueError, RuntimeError, OSError)",
        1,
    )[0]

    assert "self.pending_retry_list.append(file_path)" in timeout_block
    assert "continue" in timeout_block


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
        self.assertEqual(result.quality, "high")
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


if __name__ == "__main__":
    unittest.main()
