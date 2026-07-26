from __future__ import annotations

import unittest
from pathlib import Path

from app.services.intake_route import (
    ROUTE_EMPTY_GIF,
    ROUTE_LOCAL_FILES,
    ROUTE_LOCAL_FILES_AND_REMOTE_URLS,
    ROUTE_RAW_GIF,
    ROUTE_REMOTE_URL,
    ROUTE_REMOTE_URL_WITH_STATIC_PNG_FALLBACK,
    ROUTE_STATIC_PNG,
    ROUTE_UNSUPPORTED,
    resolve_intake_route,
)
from app.services.ingest_service import IngestService


class IntakeRouteTests(unittest.TestCase):
    def test_clipboard_priority_table(self) -> None:
        local = Path("/tmp/asset.gif")
        cases = [
            (
                {
                    "local_files": [local],
                    "remote_urls": [],
                    "raw_gif": b"GIF89a",
                    "has_image": True,
                },
                ROUTE_LOCAL_FILES,
            ),
            (
                {
                    "local_files": [local],
                    "remote_urls": ["https://cdn.example/a.gif"],
                    "raw_gif": None,
                    "has_image": False,
                },
                ROUTE_LOCAL_FILES_AND_REMOTE_URLS,
            ),
            (
                {
                    "local_files": [],
                    "remote_urls": [],
                    "raw_gif": b"GIF89a",
                    "has_image": True,
                },
                ROUTE_RAW_GIF,
            ),
            (
                {
                    "local_files": [],
                    "remote_urls": ["https://cdn.example/a.gif"],
                    "raw_gif": None,
                    "has_image": True,
                },
                ROUTE_REMOTE_URL_WITH_STATIC_PNG_FALLBACK,
            ),
            (
                {
                    "local_files": [],
                    "remote_urls": ["https://cdn.example/a.gif"],
                    "raw_gif": b"",
                    "has_image": False,
                },
                ROUTE_REMOTE_URL,
            ),
            (
                {
                    "local_files": [],
                    "remote_urls": [],
                    "raw_gif": None,
                    "has_image": True,
                },
                ROUTE_STATIC_PNG,
            ),
            (
                {
                    "local_files": [],
                    "remote_urls": [],
                    "raw_gif": b"",
                    "has_image": False,
                },
                ROUTE_EMPTY_GIF,
            ),
            (
                {
                    "local_files": [],
                    "remote_urls": [],
                    "raw_gif": None,
                    "has_image": False,
                },
                ROUTE_UNSUPPORTED,
            ),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                route = resolve_intake_route(**kwargs)
                self.assertEqual(route.name, expected)

    def test_drop_mode_ignores_clipboard_only_payloads(self) -> None:
        route = resolve_intake_route(
            local_files=[],
            remote_urls=[],
            raw_gif=b"GIF89a",
            has_image=True,
        )
        self.assertEqual(route.name, ROUTE_RAW_GIF)

        drop_route = resolve_intake_route(
            local_files=[],
            remote_urls=[],
            raw_gif=None,
            has_image=False,
        )
        self.assertEqual(drop_route.name, ROUTE_UNSUPPORTED)

        drop_remote = resolve_intake_route(
            local_files=[],
            remote_urls=["https://cdn.example/a.gif"],
            raw_gif=None,
            has_image=False,
        )
        self.assertEqual(drop_remote.name, ROUTE_REMOTE_URL)
        self.assertTrue(drop_remote.has_remote)

    def test_ingest_worker_limits_share_service_defaults(self) -> None:
        from app_gui import IngestWorker

        self.assertEqual(
            IngestWorker.MAX_FILE_SIZE_BYTES,
            IngestService.DEFAULT_MAX_FILE_BYTES,
        )
        self.assertEqual(IngestWorker.MAX_DROP_FILES, IngestService.DEFAULT_MAX_FILES)
        self.assertEqual(
            IngestWorker.MAX_DROP_BYTES,
            IngestService.DEFAULT_MAX_BATCH_BYTES,
        )
        self.assertEqual(
            IngestWorker.MIN_FREE_RESERVE_BYTES,
            IngestService.DEFAULT_RESERVE_BYTES,
        )
        self.assertEqual(
            IngestWorker.HASH_CHUNK_SIZE,
            IngestService.DEFAULT_HASH_CHUNK_SIZE,
        )


if __name__ == "__main__":
    unittest.main()
