from __future__ import annotations

from functools import lru_cache
import getpass
import os
from pathlib import Path
import secrets
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    PROJECT_NAME: str = "Haypile Asset Service"
    CORS_ORIGINS: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    DEFAULT_THEME: str = "default_theme"
    PORT: int = 8010
    HOST: str = "127.0.0.1"
    IPC_CHANNEL: str = f"haypile_service_{getpass.getuser().lower()}"
    IPC_AUTHKEY: str = ""
    HAYPILE_LOW_POWER_MODE: bool = False
    VISION_CLASSIFIER_ENABLED: bool = True
    VISION_CLASSIFIER_MODEL: str = "qwen3-vl:8b"
    VISION_CLASSIFIER_BASE_URL: str = "http://127.0.0.1:11434"
    VISION_CLASSIFIER_TIMEOUT_SECONDS: float = 8.0
    VISION_CLASSIFIER_MAX_IMAGE_BYTES: int = 8 * 1024 * 1024
    VISION_CLASSIFIER_KEEP_ALIVE: str = "30s"
    VISION_CONFIDENCE_THRESHOLD: float = 0.45
    VISION_FALLBACK_THEME: str = "generic"

    BASE_DIR: Path = Path(__file__).resolve().parents[2]
    STORAGE_DIR: Path = BASE_DIR / "storage"
    ASSETS_DIR: Path = STORAGE_DIR / "assets"
    THEMES_DIR: Path = STORAGE_DIR / "themes"
    INDEX_DIR: Path = STORAGE_DIR / "index"
    MANIFEST_PATH: Path = INDEX_DIR / "assets_manifest.json"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            text: str = value.strip()
            if not text:
                return []
            if text.startswith("[") and text.endswith("]"):
                import json

                parsed: Any = json.loads(text)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in text.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return ["http://127.0.0.1:5173", "http://localhost:5173"]

    @property
    def cors_allow_credentials(self) -> bool:
        return bool(self.CORS_ORIGINS) and "*" not in self.CORS_ORIGINS

    @field_validator("VISION_CLASSIFIER_BASE_URL", mode="before")
    @classmethod
    def normalize_vision_base_url(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text.rstrip("/") if text else "http://127.0.0.1:11434"

    @field_validator("VISION_CLASSIFIER_TIMEOUT_SECONDS", mode="before")
    @classmethod
    def clamp_vision_timeout(cls, value: Any) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return 8.0
        return timeout if timeout >= 0.5 else 0.5

    @field_validator("VISION_CLASSIFIER_MAX_IMAGE_BYTES", mode="before")
    @classmethod
    def clamp_vision_max_image_bytes(cls, value: Any) -> int:
        try:
            size = int(value)
        except (TypeError, ValueError):
            return 8 * 1024 * 1024
        return max(1024, min(size, 64 * 1024 * 1024))

    @field_validator("VISION_CLASSIFIER_KEEP_ALIVE", mode="before")
    @classmethod
    def normalize_vision_keep_alive(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text or "30s"

    @field_validator("VISION_CONFIDENCE_THRESHOLD", mode="before")
    @classmethod
    def clamp_vision_confidence_threshold(cls, value: Any) -> float:
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            return 0.45
        if threshold < 0.0:
            return 0.0
        if threshold > 1.0:
            return 1.0
        return threshold

    @field_validator("VISION_FALLBACK_THEME", mode="before")
    @classmethod
    def normalize_fallback_theme(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        return text or "generic"

    @field_validator("HOST", mode="before")
    @classmethod
    def normalize_host(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text or "127.0.0.1"

    @field_validator("IPC_CHANNEL", mode="before")
    @classmethod
    def normalize_ipc_channel(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        sanitized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)
        sanitized = sanitized.strip("_")
        return sanitized or f"haypile_service_{getpass.getuser().lower()}"

    @field_validator("IPC_AUTHKEY", mode="before")
    @classmethod
    def normalize_ipc_authkey(cls, value: Any) -> str:
        text = str(value or "").strip()
        if text and text not in {"doraemon-ipc-v1", "haypile-ipc-v1"}:
            return text
        admin_key = os.environ.get("ADMIN_API_KEY", "").strip()
        if admin_key:
            return admin_key
        return _read_or_create_ipc_authkey()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _read_or_create_ipc_authkey() -> str:
    key_path = Path(
        os.environ.get(
            "HAYPILE_IPC_AUTHKEY_FILE",
            os.environ.get(
                "DORAEMON_IPC_AUTHKEY_FILE",
                str(Path(__file__).resolve().parents[2] / "storage" / "ipc_authkey"),
            ),
        )
    )
    try:
        existing = key_path.read_text(encoding="utf-8").strip()
        if existing and existing not in {"doraemon-ipc-v1", "haypile-ipc-v1"}:
            return existing
    except OSError:
        pass
    token = secrets.token_hex(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(token, encoding="ascii")
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    return token
