from __future__ import annotations

import unittest

from app.core.config import Settings


class CorsConfigTests(unittest.TestCase):
    def test_wildcard_origin_disables_credentials(self) -> None:
        settings = Settings(_env_file=None, CORS_ORIGINS=["*"])
        self.assertFalse(settings.cors_allow_credentials)

    def test_explicit_origins_enable_credentials(self) -> None:
        settings = Settings(
            _env_file=None,
            CORS_ORIGINS=["http://127.0.0.1:5173", "http://localhost:5173"],
        )
        self.assertTrue(settings.cors_allow_credentials)


if __name__ == "__main__":
    unittest.main()
