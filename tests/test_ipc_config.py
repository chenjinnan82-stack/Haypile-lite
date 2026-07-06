from __future__ import annotations

import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

from app.core.config import Settings
from app.core import ipc


class IpcConfigTests(unittest.TestCase):
    def test_ipc_authkey_defaults_to_local_secret_file(self) -> None:
        with TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "ipc_authkey"
            with patch.dict("os.environ", {"HAYPILE_IPC_AUTHKEY_FILE": str(key_file), "IPC_AUTHKEY": ""}, clear=False):
                first = Settings(_env_file=None).IPC_AUTHKEY
                second = Settings(_env_file=None, IPC_AUTHKEY="  ").IPC_AUTHKEY

        self.assertNotEqual(first, "doraemon-ipc-v1")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_ipc_channel_defaults_to_haypile_name(self) -> None:
        self.assertTrue(Settings(_env_file=None).IPC_CHANNEL.startswith("haypile_service_"))

    def test_low_power_and_keep_alive_have_battery_friendly_defaults(self) -> None:
        settings = Settings(_env_file=None)

        self.assertFalse(settings.HAYPILE_LOW_POWER_MODE)
        self.assertEqual(settings.VISION_CLASSIFIER_KEEP_ALIVE, "30s")

    def test_ipc_authkey_uses_admin_key_when_available(self) -> None:
        with patch.dict("os.environ", {"ADMIN_API_KEY": "admin-secret", "IPC_AUTHKEY": ""}, clear=False):
            self.assertEqual(Settings(_env_file=None).IPC_AUTHKEY, "admin-secret")

    def test_send_ipc_request_uses_configured_authkey_and_logs_failure(self) -> None:
        settings = Settings(_env_file=None, IPC_AUTHKEY="local-secret")

        with (
            patch("app.core.ipc.get_settings", return_value=settings),
            patch("app.core.ipc.Client", side_effect=OSError("no listener")) as client,
            self.assertLogs("app.core.ipc", level="DEBUG") as logs,
        ):
            result = ipc.send_ipc_request({"action": "ping"}, address="local-address", timeout=0.01)

        self.assertIsNone(result)
        self.assertEqual(client.call_args.kwargs["authkey"], b"local-secret")
        self.assertTrue(any("Haypile IPC request failed" in line for line in logs.output))

    def test_start_ipc_listener_uses_configured_authkey(self) -> None:
        settings = Settings(_env_file=None, IPC_AUTHKEY="listener-secret")

        with (
            patch("app.core.ipc.get_settings", return_value=settings),
            patch("app.core.ipc.cleanup_unix_socket"),
            patch("app.core.ipc.Listener") as listener,
        ):
            ipc.start_ipc_listener(address="local-address")

        self.assertEqual(listener.call_args.kwargs["authkey"], b"listener-secret")


if __name__ == "__main__":
    unittest.main()
