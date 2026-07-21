from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services.ai_provider import (
    AIProviderConfig,
    SystemCredentialStore,
    api_authority,
    chat_completions_url,
    normalize_api_base_url,
)
from app.services.style_classifier import StyleClassifier


class AIProviderTests(unittest.TestCase):
    def test_remote_api_requires_clean_https_url(self) -> None:
        self.assertEqual(normalize_api_base_url("https://vision.example/v1/"), "https://vision.example/v1")
        self.assertEqual(api_authority("https://vision.example:8443/v1"), "vision.example:8443")
        self.assertEqual(chat_completions_url("https://vision.example/v1"), "https://vision.example/v1/chat/completions")
        self.assertEqual(chat_completions_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080/v1/chat/completions")
        for invalid in (
            "http://vision.example/v1",
            "https://user:secret@vision.example/v1",
            "https://vision.example/v1?token=secret",
            "https://vision.example/v1#fragment",
            "https://vision.example/a/../v1",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_api_base_url(invalid)

    def test_openai_compatible_payload_contains_no_filename_or_local_path(self) -> None:
        classifier = StyleClassifier(
            AIProviderConfig(
                mode="api",
                base_url="https://vision.example/v1",
                model="vision-model",
                api_key="session-secret",
                authorized_host="vision.example",
            )
        )
        payload = classifier._build_request_payload(
            "format=png; width=800; height=600",
            "aW1hZ2U=",
            "image/png",
        )
        text = str(payload)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("secret", text)
        self.assertIn("data:image/png;base64,aW1hZ2U=", text)

    def test_openai_compatible_call_uses_bearer_header_and_choices(self) -> None:
        classifier = StyleClassifier(
            AIProviderConfig(
                mode="api",
                base_url="https://vision.example/v1",
                model="vision-model",
                api_key="session-secret",
                authorized_host="vision.example",
            )
        )
        classifier._post_model_once = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": " classified "}}]},
            )
        )
        classifier._post_ollama_with_retry = AsyncMock(side_effect=AssertionError("API must not retry"))

        content, receipt = asyncio.run(classifier._call_model({"messages": []}))

        endpoint, _payload = classifier._post_model_once.await_args.args
        headers = classifier._post_model_once.await_args.kwargs["headers"]
        self.assertEqual(endpoint, "https://vision.example/v1/chat/completions")
        self.assertEqual(headers, {"Authorization": "Bearer session-secret"})
        self.assertEqual(content, "classified")
        self.assertEqual(receipt, {})
        classifier._post_ollama_with_retry.assert_not_awaited()

    def test_api_classifier_rejects_an_unapproved_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "api_host_not_authorized"):
            StyleClassifier(
                AIProviderConfig(
                    mode="api",
                    base_url="https://other.example/v1",
                    model="vision-model",
                    api_key="session-secret",
                    authorized_host="vision.example",
                )
            )

    def test_keychain_failure_returns_no_plaintext_fallback(self) -> None:
        with patch("app.services.ai_provider.sys.platform", "darwin"), patch(
            "app.services.ai_provider.subprocess.run"
        ) as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            self.assertFalse(SystemCredentialStore.set("vision.example", "secret"))
            self.assertEqual(SystemCredentialStore.get("vision.example"), "")


if __name__ == "__main__":
    unittest.main()
