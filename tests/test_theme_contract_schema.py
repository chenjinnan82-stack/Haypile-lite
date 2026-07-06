from __future__ import annotations

import unittest

from app.models.theme import AestheticPayload


class ThemeContractSchemaTests(unittest.TestCase):
    def test_aesthetic_payload_uses_frontend_expected_fields(self) -> None:
        payload = AestheticPayload.model_validate(
            {
                "theme_name": "generic",
                "css_variables": {"--bg-primary": "#FFFFFF"},
                "tailwind_extend": {"colors": {"brand": "#00A0E9"}},
                "fonts": ["https://example.com/font.css"],
                "physical_assets": {
                    "main_background": {
                        "url": "http://127.0.0.1:8010/static/generic/bg.png",
                        "type": "background",
                        "resolution": "1920x1080",
                        "aspect_ratio": "1.7778",
                        "css_advice": "bg-cover bg-center",
                        "placement_intent": "full screen background",
                    }
                },
                "ui_dev_instruction": "keep readable",
            }
        )

        dumped = payload.model_dump()
        self.assertEqual(
            sorted(dumped.keys()),
            sorted(
                [
                    "theme_name",
                    "css_variables",
                    "tailwind_extend",
                    "fonts",
                    "physical_assets",
                    "ui_dev_instruction",
                ]
            ),
        )
        self.assertIn("main_background", dumped["physical_assets"])


if __name__ == "__main__":
    unittest.main()
