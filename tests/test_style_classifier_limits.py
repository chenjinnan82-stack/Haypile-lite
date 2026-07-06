from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
