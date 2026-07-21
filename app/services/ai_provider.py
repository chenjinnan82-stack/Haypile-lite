from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class AIProviderConfig:
    mode: str = "off"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    authorized_host: str = ""

    @property
    def enabled(self) -> bool:
        return self.mode in {"local", "api"}


def normalize_api_base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("invalid_api_url")
    if hostname not in LOCAL_HOSTS and parsed.scheme != "https":
        raise ValueError("remote_api_requires_https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("api_url_credentials_not_allowed")
    if parsed.query or parsed.fragment:
        raise ValueError("api_url_query_or_fragment_not_allowed")
    if any(part == ".." for part in parsed.path.split("/")):
        raise ValueError("api_url_parent_path_not_allowed")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def api_authority(base_url: str) -> str:
    parsed = urlparse(normalize_api_base_url(base_url))
    hostname = (parsed.hostname or "").lower()
    return f"{hostname}:{parsed.port}" if parsed.port is not None else hostname


def chat_completions_url(base_url: str) -> str:
    normalized = normalize_api_base_url(base_url)
    return (
        f"{normalized}/chat/completions"
        if urlparse(normalized).path.rstrip("/").endswith("/v1")
        else f"{normalized}/v1/chat/completions"
    )


class SystemCredentialStore:
    SERVICE = "Haypile AI API"
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    @classmethod
    def set(cls, account: str, secret: str) -> bool:
        if not account or not secret:
            return False
        if sys.platform == "darwin":
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-a",
                    account,
                    "-s",
                    cls.SERVICE,
                    "-w",
                    secret,
                    "-U",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return result.returncode == 0
        if sys.platform.startswith("win"):
            return cls._windows_set(account, secret)
        return False

    @classmethod
    def get(cls, account: str) -> str:
        if not account:
            return ""
        if sys.platform == "darwin":
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-a",
                    account,
                    "-s",
                    cls.SERVICE,
                    "-w",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        if sys.platform.startswith("win"):
            return cls._windows_get(account)
        return ""

    @classmethod
    def delete(cls, account: str) -> bool:
        if not account:
            return False
        if sys.platform == "darwin":
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "delete-generic-password",
                    "-a",
                    account,
                    "-s",
                    cls.SERVICE,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return result.returncode == 0
        if sys.platform.startswith("win"):
            _credential_type, _write, _read, cred_delete, _free = cls._windows_api()
            return bool(cred_delete(cls._windows_target(account), cls.CRED_TYPE_GENERIC, 0))
        return False

    @classmethod
    def _windows_target(cls, account: str) -> str:
        return f"{cls.SERVICE}:{account}"

    @classmethod
    def _windows_set(cls, account: str, secret: str) -> bool:
        credential_type, cred_write, _read, _delete, _free = cls._windows_api()
        blob = secret.encode("utf-16-le")
        blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = credential_type()
        credential.Type = cls.CRED_TYPE_GENERIC
        credential.TargetName = cls._windows_target(account)
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = cls.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = account
        return bool(cred_write(ctypes.byref(credential), 0))

    @classmethod
    def _windows_get(cls, account: str) -> str:
        credential_type, _write, cred_read, _delete, cred_free = cls._windows_api()
        pointer = ctypes.POINTER(credential_type)()
        if not cred_read(
            cls._windows_target(account), cls.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            return ""
        try:
            credential = pointer.contents
            if not credential.CredentialBlob or credential.CredentialBlobSize <= 0:
                return ""
            return ctypes.string_at(
                credential.CredentialBlob, credential.CredentialBlobSize
            ).decode("utf-16-le")
        finally:
            cred_free(pointer)

    @classmethod
    def _windows_api(cls):
        credential_type = cls._windows_credential_type()
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        cred_write = advapi.CredWriteW
        cred_write.argtypes = [ctypes.POINTER(credential_type), wintypes.DWORD]
        cred_write.restype = wintypes.BOOL
        cred_read = advapi.CredReadW
        cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(credential_type)),
        ]
        cred_read.restype = wintypes.BOOL
        cred_delete = advapi.CredDeleteW
        cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        cred_delete.restype = wintypes.BOOL
        cred_free = advapi.CredFree
        cred_free.argtypes = [ctypes.c_void_p]
        cred_free.restype = None
        return credential_type, cred_write, cred_read, cred_delete, cred_free

    @staticmethod
    def _windows_credential_type():
        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        return Credential
