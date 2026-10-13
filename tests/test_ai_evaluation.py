from __future__ import annotations

import argparse
import io
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from app.core.config import get_settings
from app.services.ai_provider import AIProviderConfig
from app.services.style_classifier import StyleClassificationResult, StyleClassifier


EVALUATOR = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "evaluate_image_sorting.py")
)
SUMMARIZE = EVALUATOR["summarize"]
EVALUATE = EVALUATOR["evaluate"]
LOAD_SAMPLES = EVALUATOR["load_samples"]
MAIN = EVALUATOR["main"]


class AIEvaluationTests(unittest.TestCase):
    def test_release_gate_uses_accuracy_and_reports_coverage(self) -> None:
        records = [
            {
                "auto_ready": index < 40,
                "correct": index != 39,
                "source": "model",
                "elapsed_seconds": index + 1.0,
            }
            for index in range(100)
        ]

        summary = SUMMARIZE(records)

        self.assertEqual(summary["auto_ready_count"], 40)
        self.assertEqual(summary["auto_ready_accuracy"], 0.975)
        self.assertEqual(summary["coverage"], 0.4)
        self.assertEqual(summary["failure_count"], 0)
        self.assertEqual(summary["actual_model_result_count"], 100)
        self.assertEqual(summary["timed_sample_count"], 100)
        self.assertEqual(summary["elapsed_seconds_median"], 50.5)
        self.assertEqual(summary["elapsed_seconds_p95"], 95.0)
        self.assertEqual(summary["elapsed_seconds_max"], 100.0)
        self.assertEqual(summary["p95_method"], "nearest_rank")
        self.assertTrue(summary["release_gate_passed"])

    def test_summary_excludes_fallbacks_even_if_marked_correct_and_ready(self) -> None:
        records = [
            {"auto_ready": True, "correct": True, "source": "model_fallback", "elapsed_seconds": 3},
            {"auto_ready": True, "correct": True, "source": "exception", "elapsed_seconds": 4},
            {"auto_ready": True, "correct": True, "source": "model", "elapsed_seconds": 1},
            {"auto_ready": False, "correct": True, "source": "model", "elapsed_seconds": 2},
        ]

        summary = SUMMARIZE(records)

        self.assertEqual(summary["failure_count"], 2)
        self.assertEqual(summary["actual_model_result_count"], 2)
        self.assertEqual(summary["auto_ready_count"], 1)
        self.assertEqual(summary["auto_ready_correct"], 1)
        self.assertEqual(summary["coverage"], 0.25)
        self.assertEqual(summary["elapsed_seconds_median"], 2.5)
        self.assertEqual(summary["elapsed_seconds_p95"], 4)
        self.assertEqual(summary["elapsed_seconds_max"], 4)

    def test_empty_input_never_passes_release_gate(self) -> None:
        summary = SUMMARIZE([])

        self.assertFalse(summary["release_gate_passed"])
        self.assertEqual(summary["sample_count"], 0)
        self.assertEqual(summary["failure_count"], 0)
        self.assertEqual(summary["actual_model_result_count"], 0)
        self.assertEqual(summary["timed_sample_count"], 0)
        self.assertEqual(summary["elapsed_seconds_median"], 0)
        self.assertEqual(summary["elapsed_seconds_p95"], 0)
        self.assertEqual(summary["elapsed_seconds_max"], 0)

    def test_legacy_summary_records_keep_their_gate_behavior(self) -> None:
        records = [{"auto_ready": True, "correct": True} for _ in range(80)]

        summary = SUMMARIZE(records)

        self.assertTrue(summary["release_gate_passed"])
        self.assertEqual(summary["actual_model_result_count"], 80)
        self.assertEqual(summary["timed_sample_count"], 0)


class AIEvaluationRunTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def result(source: str = "model", role: str = "content_image") -> StyleClassificationResult:
        return StyleClassificationResult(
            theme_id="generic",
            theme_confidence=0,
            role_confidence=0.95,
            role=role,
            source=source,
            reason="test_result",
            quality="high",
            quality_reason="test_quality",
        )

    async def test_exception_records_failure_and_continues_with_next_image(self) -> None:
        samples = [
            (Path("timeout.png"), "timeout.png", "unknown"),
            (Path("valid.png"), "valid.png", "content_image"),
        ]
        with (
            patch.object(
                StyleClassifier,
                "classify_image",
                AsyncMock(side_effect=[httpx.TimeoutException("secret request URL"), self.result()]),
            ) as classify,
            patch.object(EVALUATE.__globals__["time"], "perf_counter", side_effect=[0, 2, 3, 6]),
        ):
            records = await EVALUATE(samples, AIProviderConfig(mode="local"))

        self.assertEqual(classify.await_count, 2)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["source"], "exception")
        self.assertEqual(records[0]["error_type"], "TimeoutException")
        self.assertEqual(records[0]["elapsed_seconds"], 2)
        self.assertFalse(records[0]["correct"])
        self.assertFalse(records[0]["auto_ready"])
        self.assertNotIn("secret", json.dumps(records))
        self.assertEqual(records[1]["source"], "model")
        self.assertEqual(records[1]["elapsed_seconds"], 3)
        self.assertTrue(records[1]["correct"])
        self.assertTrue(records[1]["auto_ready"])

    async def test_unknown_fallbacks_are_not_correct_model_results(self) -> None:
        sources = ["model_fallback", "disabled", "guard", "model"]
        samples = [(Path(f"{source}.png"), f"{source}.png", "unknown") for source in sources]
        with patch.object(
            StyleClassifier,
            "classify_image",
            AsyncMock(side_effect=[self.result(source, "unknown") for source in sources]),
        ):
            records = await EVALUATE(samples, AIProviderConfig(mode="local"))

        self.assertEqual([record["correct"] for record in records], [False, False, False, True])
        self.assertFalse(any(record["auto_ready"] for record in records))
        self.assertTrue(all(record["elapsed_seconds"] >= 0 for record in records))
        summary = SUMMARIZE(records)
        self.assertEqual(summary["failure_count"], 3)
        self.assertEqual(summary["actual_model_result_count"], 1)

    async def test_timeout_override_is_local_to_diagnostic_classifier(self) -> None:
        configured_timeout = get_settings().VISION_CLASSIFIER_TIMEOUT_SECONDS
        observed_timeouts = []

        async def classify(classifier, _path):
            observed_timeouts.append(classifier.timeout_seconds)
            return self.result()

        with patch.object(StyleClassifier, "classify_image", classify):
            await EVALUATE(
                [(Path("valid.png"), "valid.png", "content_image")],
                AIProviderConfig(mode="local"),
                timeout_seconds=42.5,
            )

        self.assertEqual(observed_timeouts, [42.5])
        self.assertEqual(get_settings().VISION_CLASSIFIER_TIMEOUT_SECONDS, configured_timeout)

    async def test_invalid_timeout_override_is_rejected(self) -> None:
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "timeout_seconds"):
                await EVALUATE([], AIProviderConfig(mode="local"), timeout_seconds=timeout)


class AIEvaluationDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.labels = self.root / "labels.json"

    def load(self, files: list[str]):
        self.labels.write_text(
            json.dumps({"samples": [{"file": name, "role": "content_image"} for name in files]}),
            encoding="utf-8",
        )
        return LOAD_SAMPLES(self.root, self.labels)

    def test_supported_real_png_jpeg_and_webp_images_load(self) -> None:
        names = ["image.png", "image.jpg", "image.jpeg", "image.webp"]
        for index, name in enumerate(names):
            with Image.new("RGB", (8 + index, 8), (index, 20, 30)) as image:
                image.save(self.root / name)

        samples = self.load(names)

        self.assertEqual([sample[1] for sample in samples], names)

    def test_duplicate_file_alias_is_rejected(self) -> None:
        with Image.new("RGB", (8, 8)) as image:
            image.save(self.root / "image.png")
        with self.assertRaisesRegex(ValueError, "duplicate sample file"):
            self.load(["image.png", "./image.png"])

    def test_duplicate_content_with_different_filename_is_rejected(self) -> None:
        with Image.new("RGB", (8, 8)) as image:
            image.save(self.root / "image.png")
        (self.root / "copy.png").write_bytes((self.root / "image.png").read_bytes())

        with self.assertRaisesRegex(ValueError, "duplicate sample content"):
            self.load(["image.png", "copy.png"])

    def test_unsupported_format_and_fake_raster_are_rejected(self) -> None:
        (self.root / "image.svg").write_text("<svg/>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported evaluation image"):
            self.load(["image.svg"])
        (self.root / "fake.png").write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "invalid evaluation image"):
            self.load(["fake.png"])

    def test_raster_content_must_match_extension(self) -> None:
        with Image.new("RGB", (8, 8)) as image:
            image.save(self.root / "wrong.png", format="JPEG")

        with self.assertRaisesRegex(ValueError, "raster_extension_mismatch"):
            self.load(["wrong.png"])


class AIEvaluationReportTests(unittest.TestCase):
    def test_report_includes_provider_and_timeout_without_api_credentials_or_url(self) -> None:
        settings = SimpleNamespace(
            VISION_CLASSIFIER_BASE_URL="https://private.example/v1",
            VISION_CLASSIFIER_MODEL="vision-test",
            VISION_CLASSIFIER_TIMEOUT_SECONDS=8.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "evaluation.json"
            args = argparse.Namespace(
                dataset=Path(tmp),
                labels=Path(tmp) / "labels.json",
                mode="api",
                base_url="",
                model="",
                output=output,
                timeout_seconds=42.5,
                enforce_release_gate=True,
            )
            evaluate = AsyncMock(return_value=[])
            with (
                patch.dict(
                    MAIN.__globals__,
                    {
                        "parse_args": lambda: args,
                        "get_settings": lambda: settings,
                        "load_samples": lambda *_args: [],
                        "evaluate": evaluate,
                    },
                ),
                patch.dict("os.environ", {"HAYPILE_EVAL_API_KEY": "dummy-test-secret"}),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                code = MAIN()

            rendered = output.read_text(encoding="utf-8")
            report = json.loads(rendered)
            console = json.loads(stdout.getvalue())

        self.assertEqual(code, 1)
        self.assertEqual(
            report["provider"],
            {
                "mode": "api",
                "model": "vision-test",
                "transport": "openai",
                "configured_timeout_seconds": 8.0,
                "timeout_seconds": 42.5,
            },
        )
        self.assertEqual(console["provider"], report["provider"])
        self.assertEqual(evaluate.await_args.args[2], 42.5)
        self.assertEqual(settings.VISION_CLASSIFIER_TIMEOUT_SECONDS, 8.0)
        for secret in ("dummy-test-secret", "private.example"):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
