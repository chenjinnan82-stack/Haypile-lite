from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
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

        self.assertNotEqual(first, "haypile-ipc-v1")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_ipc_channel_defaults_to_haypile_name(self) -> None:
        self.assertTrue(Settings(_env_file=None).IPC_CHANNEL.startswith("haypile_service_"))

    def test_low_power_and_keep_alive_have_battery_friendly_defaults(self) -> None:
        settings = Settings(_env_file=None)

        self.assertFalse(settings.HAYPILE_LOW_POWER_MODE)
        self.assertEqual(settings.VISION_CLASSIFIER_TRANSPORT, "ollama")
        self.assertEqual(settings.VISION_CLASSIFIER_KEEP_ALIVE, "30s")

    def test_sophon_transport_keeps_gateway_local(self) -> None:
        settings = Settings(
            _env_file=None,
            VISION_CLASSIFIER_TRANSPORT="SOPHON",
            SOPHON_BASE_URL="http://localhost:8030/",
        )
        invalid = Settings(_env_file=None, SOPHON_BASE_URL="https://example.com")

        self.assertEqual(settings.VISION_CLASSIFIER_TRANSPORT, "sophon")
        self.assertEqual(settings.SOPHON_BASE_URL, "http://localhost:8030")
        self.assertEqual(invalid.SOPHON_BASE_URL, "http://127.0.0.1:8030")

    def test_ipc_authkey_uses_admin_key_when_available(self) -> None:
        with patch.dict("os.environ", {"ADMIN_API_KEY": "admin-secret", "IPC_AUTHKEY": ""}, clear=False):
            self.assertEqual(Settings(_env_file=None).IPC_AUTHKEY, "admin-secret")

    def test_send_ipc_request_uses_configured_authkey_and_logs_failure(self) -> None:
        settings = Settings(_env_file=None, IPC_AUTHKEY="local-secret")
        raw_socket = MagicMock()
        raw_socket.detach.return_value = 42
        connection = MagicMock()
        connection.poll.return_value = False

        with (
            patch("app.core.ipc.get_settings", return_value=settings),
            patch("app.core.ipc.get_listener_family", return_value="AF_UNIX"),
            patch("app.core.ipc.socket.socket", return_value=raw_socket),
            patch("app.core.ipc.Connection", return_value=connection),
            patch("app.core.ipc.authenticate_ipc_connection") as authenticate,
        ):
            result = ipc.send_ipc_request({"action": "ping"}, address="local-address", timeout=0.01)

        self.assertIsNone(result)
        self.assertEqual(raw_socket.settimeout.call_args_list[0].args, (0.01,))
        self.assertEqual(raw_socket.settimeout.call_args_list[1].args, (None,))
        authenticate.assert_called_once_with(
            connection,
            b"local-secret",
            timeout=0.01,
            server=False,
        )
        connection.close.assert_called_once_with()

    @unittest.skipUnless(ipc.is_windows(), "Windows named pipe test")
    def test_windows_ipc_waits_for_named_pipe_with_request_timeout(self) -> None:
        connection = MagicMock()
        connection.poll.return_value = False
        with (
            patch("_winapi.WaitNamedPipe") as wait_named_pipe,
            patch("app.core.ipc.Client", return_value=connection),
            patch("app.core.ipc.authenticate_ipc_connection"),
        ):
            result = ipc.send_ipc_request(
                {"action": "ping"},
                address=r"\\.\pipe\haypile-test",
                authkey=b"local-secret",
                timeout=0.01,
            )

        self.assertIsNone(result)
        wait_named_pipe.assert_called_once_with(r"\\.\pipe\haypile-test", 10)
        connection.close.assert_called_once_with()

    def test_start_ipc_listener_defers_authentication_to_timed_connection_handler(self) -> None:
        with (
            patch("app.core.ipc.cleanup_unix_socket"),
            patch("app.core.ipc.Listener") as listener,
        ):
            ipc.start_ipc_listener(address="local-address")

        self.assertIsNone(listener.call_args.kwargs["authkey"])


if __name__ == "__main__":
    unittest.main()
