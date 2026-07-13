from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

import httpx
from PIL import Image, UnidentifiedImageError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import get_settings
from app.core.exceptions import ResourceExhaustedError
from app.core.limiter import ConcurrencyLimiter

logger = logging.getLogger(__name__)


def _log_timeout_retry(retry_state: Any) -> None:
    attempt_no = retry_state.attempt_number + 1
    logger.warning(
        "[StyleClassifier] Ollama 响应超时，正在准备第 %s 次重试... 模型加载中请耐心等待",
        attempt_no,
    )


@dataclass(slots=True)
class StyleClassificationResult:
    theme_id: str
    theme_confidence: float
    role_confidence: float
    role: str
    source: str
    reason: str
    tags: list[str] = field(default_factory=list)
    quality: str = "unknown"
    agent_summary: str = ""
    runtime_receipt: dict[str, Any] = field(default_factory=dict)

    @property
    def confidence(self) -> float:
        # Backward-compatible projection for legacy callers.
        return self.theme_confidence

    def ai_suggestions(self) -> dict[str, Any]:
        suggestions = {
            "source": self.source,
            "tags": self.tags,
            "usage": self.role,
            "quality": self.quality,
            "agent_summary": self.agent_summary,
            "confidence": {
                "theme": self.theme_confidence,
                "role": self.role_confidence,
            },
            "reason": self.reason,
        }
        if self.runtime_receipt:
            suggestions["runtime_receipt"] = self.runtime_receipt
        return suggestions


class StyleClassifier:
    """
    Vision-based style classifier for pre-ingest theme routing.

    Design goals:
    - Never block ingest pipeline on model failures.
    - Return deterministic fallback when model is unavailable or response is invalid.
    - Keep output schema strict and stable.
    """

    ALLOWED_IMAGE_SUFFIXES: set[str] = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
    HTTP_TIMEOUT = httpx.Timeout(120.0, connect=2.0, read=115.0, pool=2.0)

    def __init__(self) -> None:
        settings = get_settings()
        self.low_power_mode: bool = bool(settings.HAYPILE_LOW_POWER_MODE)
        self.enabled: bool = bool(settings.VISION_CLASSIFIER_ENABLED) and not self.low_power_mode
        self.transport: str = settings.VISION_CLASSIFIER_TRANSPORT
        self.model: str = settings.VISION_CLASSIFIER_MODEL
        self.base_url: str = settings.VISION_CLASSIFIER_BASE_URL.rstrip("/")
        self.sophon_base_url: str = settings.SOPHON_BASE_URL.rstrip("/")
        self.timeout_seconds: float = float(settings.VISION_CLASSIFIER_TIMEOUT_SECONDS)
        self.max_image_bytes: int = int(settings.VISION_CLASSIFIER_MAX_IMAGE_BYTES)
        self.keep_alive: str = str(settings.VISION_CLASSIFIER_KEEP_ALIVE).strip()
        self.confidence_threshold: float = float(settings.VISION_CONFIDENCE_THRESHOLD)
        self.fallback_theme: str = (
            str(settings.VISION_FALLBACK_THEME).strip().lower() or "generic"
        )
        self._limiter = ConcurrencyLimiter.from_cpu_count()

    async def classify_image(
        self,
        image_path: Path,
        candidate_themes: list[str] | None = None,
    ) -> StyleClassificationResult:
        """
        Classify an image into a theme bucket and a basic asset role.

        Returns fallback result if:
        - classifier is disabled,
        - input is invalid,
        - request fails,
        - response is malformed,
        - confidence is below threshold.
        """
        normalized_candidates = self._normalize_candidate_themes(candidate_themes)

        if getattr(self, "low_power_mode", False):
            return self._fallback_result(
                reason="low_power_mode",
                role="unknown",
                source="disabled",
            )

        if not self.enabled:
            return self._fallback_result(
                reason="classifier_disabled",
                role="unknown",
                source="disabled",
            )

        if not image_path.exists() or not image_path.is_file():
            return self._fallback_result(
                reason="missing_image_file",
                role="unknown",
                source="guard",
            )

        if image_path.suffix.lower() not in self.ALLOWED_IMAGE_SUFFIXES:
            return self._fallback_result(
                reason="unsupported_image_suffix",
                role="unknown",
                source="guard",
            )

        try:
            if image_path.stat().st_size > self.max_image_bytes:
                return self._fallback_result(
                    reason="image_too_large",
                    role="unknown",
                    source="guard",
                )
        except OSError:
            return self._fallback_result(
                reason="image_stat_failed",
                role="unknown",
                source="guard",
            )

        image_b64 = self._encode_image_base64(image_path)
        if not image_b64:
            return self._fallback_result(
                reason="image_encode_failed",
                role="unknown",
                source="guard",
            )

        metadata = self._collect_image_metadata(image_path)
        prompt = self._build_prompt(normalized_candidates, metadata)
        payload = self._build_request_payload(prompt=prompt, image_b64=image_b64)

        raw, runtime_receipt = await self._call_model(payload)
        if not raw:
            result = self._fallback_result(
                reason="model_call_failed",
                role="unknown",
                source="model_fallback",
            )
            result.runtime_receipt = runtime_receipt
            return result

        parsed = self._parse_model_json(raw)
        if parsed is None:
            result = self._fallback_result(
                reason="model_json_parse_failed",
                role="unknown",
                source="model_fallback",
            )
            result.runtime_receipt = runtime_receipt
            return result

        result = self._normalize_result(parsed, normalized_candidates)
        result.runtime_receipt = runtime_receipt
        if result.theme_confidence < self.confidence_threshold:
            return StyleClassificationResult(
                theme_id=self.fallback_theme,
                theme_confidence=result.theme_confidence,
                role_confidence=result.role_confidence,
                role=result.role,
                source="threshold_theme_fallback",
                reason=f"low_theme_confidence:{result.theme_confidence:.3f}",
                tags=result.tags,
                quality=result.quality,
                agent_summary=result.agent_summary,
                runtime_receipt=runtime_receipt,
            )

        return result

    def _normalize_candidate_themes(
        self, candidate_themes: list[str] | None
    ) -> list[str]:
        if not candidate_themes:
            return [self.fallback_theme]
        normalized: list[str] = []
        for theme in candidate_themes:
            text = str(theme or "").strip().lower()
            if not text:
                continue
            if text not in normalized:
                normalized.append(text)
        if self.fallback_theme not in normalized:
            normalized.append(self.fallback_theme)
        return normalized or [self.fallback_theme]

    @staticmethod
    def _encode_image_base64(image_path: Path) -> str:
        try:
            binary = image_path.read_bytes()
        except OSError:
            return ""
        if not binary:
            return ""
        return base64.b64encode(binary).decode("utf-8")

    def _build_prompt(
        self, candidate_themes: list[str], metadata: dict[str, Any]
    ) -> str:
        themes_text = ", ".join(candidate_themes)
        metadata_text = self._format_metadata_text(metadata)
        return (
            "你是 Haypile 入库视觉分拣官（Visual Intake Curator）。\n"
            "目标：对单张图片给出【主题归属 + 资产角色】，服务后续自动入库与主题合成。\n"
            "你必须保守判断：宁可降低置信度，也不要编造主题或角色。\n"
            "\n"
            "硬性规则：\n"
            "1) theme_id 只能从候选主题里选；禁止创造新主题名。\n"
            "2) 若无法稳定判断主题，theme_id 必须返回 fallback 主题。\n"
            "3) role 只能是：main_background | hero_image | icon | texture | unknown。\n"
            "4) theme_confidence 与 role_confidence 都必须是 0.0~1.0 的浮点数。\n"
            "5) reason 必须是简短中文短语，说明判定依据，不超过20字。\n"
            "6) tags 给 2~6 个短标签，描述内容/风格/色彩/用途。\n"
            "7) quality 只能是：high | medium | low | unknown。\n"
            "8) agent_summary 用一句中文告诉 agent 这个素材适合怎么用，不超过60字。\n"
            "\n"
            "角色判定边界：\n"
            "- main_background：大面积环境底图/场景背景，适合铺底。\n"
            "- hero_image：视觉主体，通常是单个核心对象或主角元素。\n"
            "- icon：小尺寸符号化图形，轮廓清晰，用于功能标识。\n"
            "- texture：重复纹理/材质细节，常用于叠加或填充。\n"
            "- unknown：当类别冲突、信息不足或质量太差时使用。\n"
            "\n"
            "输出必须是严格 JSON，不要输出任何解释、代码块或额外文本。\n"
            "JSON schema:\n"
            "{\n"
            '  "theme_id": "<candidate_theme_id>",\n'
            '  "theme_confidence": 0.0,\n'
            '  "role_confidence": 0.0,\n'
            '  "role": "main_background|hero_image|icon|texture|unknown",\n'
            '  "reason": "short rationale",\n'
            '  "tags": ["tag"],\n'
            '  "quality": "high|medium|low|unknown",\n'
            '  "agent_summary": "short usage suggestion for agents"\n'
            "}\n"
            "图片元数据:\n"
            f"{metadata_text}\n"
            f"候选主题: [{themes_text}]\n"
            f"fallback 主题: {self.fallback_theme}\n"
        )

    def _build_request_payload(self, prompt: str, image_b64: str) -> dict[str, Any]:
        # Ollama-compatible chat payload with image in user message.
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是严格的视觉分拣模型。"
                        "你的输出只允许是一个JSON对象。"
                        "禁止输出markdown、禁止额外解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64],
                },
            ],
            "options": {
                "temperature": 0.0,
            },
        }
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        return payload

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=10),
        retry=retry_if_exception_type(httpx.ReadTimeout),
        before_sleep=_log_timeout_retry,
        reraise=True,
    )
    async def _post_ollama_with_retry(
        self,
        endpoint: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async with self._limiter:
            async with httpx.AsyncClient(timeout=self.HTTP_TIMEOUT, trust_env=False) as client:
                return await client.post(endpoint, json=payload, headers=headers)

    async def _call_model(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        request_id = ""
        headers: dict[str, str] | None = None
        if self.transport == "sophon":
            api_key = self._sophon_api_key()
            if not api_key:
                logger.error("Sophon vision transport requires a local admin key.")
                return "", {}
            request_id = f"haypile-vision-{uuid4().hex}"
            endpoint = f"{self.sophon_base_url}/v1/chat/completions"
            headers = {
                "X-PimOS-Admin-Key": api_key,
                "X-Sophon-Provider": "ollama",
                "X-Sophon-Client-Id": "haypile-vision",
                "X-Sophon-Project-Id": "haypile",
                "X-Request-ID": request_id,
            }
        else:
            endpoint = f"{self.base_url}/api/chat"
        try:
            response = await self._post_ollama_with_retry(endpoint, payload, headers=headers)
            receipt = await self._fetch_sophon_receipt(request_id) if request_id else {}
            if response.status_code != 200:
                logger.error(
                    "Style classifier model call failed: status=%s endpoint=%s model=%s body=%s",
                    response.status_code,
                    endpoint,
                    self.model,
                    response.text[:500],
                )
                return "", receipt
            data = response.json()
        except httpx.ReadTimeout as exc:
            logger.error(
                "Style classifier model call timeout after retries: endpoint=%s model=%s error=%r",
                endpoint,
                self.model,
                exc,
            )
            raise
        except ResourceExhaustedError:
            raise
        except httpx.TimeoutException as exc:
            logger.error(
                "Style classifier timeout exception: endpoint=%s model=%s error=%r",
                endpoint,
                self.model,
                exc,
            )
            raise
        except (httpx.RequestError, ValueError) as exc:
            logger.error(
                "Style classifier model call exception: endpoint=%s model=%s error=%r",
                endpoint,
                self.model,
                exc,
            )
            receipt = await self._fetch_sophon_receipt(request_id) if request_id else {}
            return "", receipt

        if not isinstance(data, dict):
            return "", receipt

        if self.transport == "sophon":
            choices = data.get("choices") if isinstance(data.get("choices"), list) else []
            choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = message.get("content")
            return (content.strip(), receipt) if isinstance(content, str) else ("", receipt)

        message = data.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip(), receipt

        # Backward-compatible fallback key
        content = data.get("response")
        if isinstance(content, str):
            return content.strip(), receipt

        return "", receipt

    def _sophon_api_key(self) -> str:
        configured = os.environ.get("ADMIN_API_KEY", "").strip()
        if configured:
            return configured
        key_file = os.environ.get("PIMOS_ADMIN_API_KEY_FILE", "").strip()
        if not key_file:
            return ""
        try:
            return Path(key_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    async def _fetch_sophon_receipt(self, request_id: str) -> dict[str, Any]:
        api_key = self._sophon_api_key()
        if not request_id or not api_key:
            return {}
        try:
            async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
                response = await client.get(
                    f"{self.sophon_base_url}/api/v1/sophon/model-usage/recent",
                    params={"request_id": request_id},
                    headers={"X-API-Key": api_key},
                )
            if response.status_code == 200:
                payload = response.json()
                events = payload.get("events") if isinstance(payload, dict) else None
                if isinstance(events, list) and events and isinstance(events[0], dict):
                    return events[0]
        except (httpx.RequestError, ValueError):
            pass
        return {
            "schema_version": "sophon.runtime-receipt.v1",
            "request_id": request_id,
            "status": "unavailable",
        }

    @staticmethod
    def _parse_model_json(raw_text: str) -> dict[str, Any] | None:
        text = raw_text.strip()
        if not text:
            return None

        # Direct JSON first
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        # Extract first {...} block if model wrapped with prose/code fences.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None

        snippet = text[start : end + 1]
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _normalize_result(
        self,
        payload: dict[str, Any],
        candidate_themes: list[str],
    ) -> StyleClassificationResult:
        theme_id = str(payload.get("theme_id", "")).strip().lower()
        if theme_id not in candidate_themes:
            theme_id = self.fallback_theme

        theme_confidence = self._to_confidence(
            payload.get("theme_confidence", payload.get("confidence"))
        )
        role_confidence = self._to_confidence(
            payload.get("role_confidence", payload.get("confidence"))
        )
        role = self._normalize_role(payload.get("role"))
        reason = str(payload.get("reason", "")).strip() or "model_classification"

        return StyleClassificationResult(
            theme_id=theme_id,
            theme_confidence=theme_confidence,
            role_confidence=role_confidence,
            role=role,
            source="model",
            reason=reason,
            tags=self._normalize_tags(payload.get("tags")),
            quality=self._normalize_quality(payload.get("quality")),
            agent_summary=self._short_text(payload.get("agent_summary"), 120),
        )

    @staticmethod
    def _to_confidence(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 0.0
        if score < 0.0:
            return 0.0
        if score > 1.0:
            return 1.0
        return score

    @staticmethod
    def _normalize_role(value: Any) -> str:
        role = str(value or "").strip().lower()
        allowed = {"main_background", "hero_image", "icon", "texture", "unknown"}
        return role if role in allowed else "unknown"

    @staticmethod
    def _normalize_tags(value: Any) -> list[str]:
        raw = value if isinstance(value, list) else []
        tags: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            if text and text not in tags:
                tags.append(text[:24])
            if len(tags) >= 6:
                break
        return tags

    @staticmethod
    def _normalize_quality(value: Any) -> str:
        quality = str(value or "").strip().lower()
        return quality if quality in {"high", "medium", "low", "unknown"} else "unknown"

    @staticmethod
    def _short_text(value: Any, limit: int) -> str:
        return str(value or "").strip().replace("\n", " ")[:limit]

    def _fallback_result(
        self, reason: str, role: str, source: str
    ) -> StyleClassificationResult:
        return StyleClassificationResult(
            theme_id=self.fallback_theme,
            theme_confidence=0.0,
            role_confidence=0.0,
            role=self._normalize_role(role),
            source=source,
            reason=reason,
        )

    def _collect_image_metadata(self, image_path: Path) -> dict[str, Any]:
        width, height, has_alpha = self._read_dimensions_and_alpha(image_path)
        file_size_kb = 0.0
        try:
            file_size_kb = round(image_path.stat().st_size / 1024.0, 2)
        except OSError:
            file_size_kb = 0.0
        aspect_ratio = (
            round(width / height, 4) if width is not None and height not in {None, 0} else None
        )
        return {
            "filename": image_path.name,
            "suffix": image_path.suffix.lower(),
            "file_size_kb": file_size_kb,
            "width": width,
            "height": height,
            "aspect_ratio": aspect_ratio,
            "has_alpha": has_alpha,
        }

    def _format_metadata_text(self, metadata: dict[str, Any]) -> str:
        lines = [
            f"- filename: {metadata.get('filename', 'unknown')}",
            f"- suffix: {metadata.get('suffix', 'unknown')}",
            f"- file_size_kb: {metadata.get('file_size_kb', 'unknown')}",
            f"- width: {metadata.get('width', 'unknown')}",
            f"- height: {metadata.get('height', 'unknown')}",
            f"- aspect_ratio: {metadata.get('aspect_ratio', 'unknown')}",
            f"- has_alpha: {metadata.get('has_alpha', 'unknown')}",
        ]
        return "\n".join(lines)

    def _read_dimensions_and_alpha(
        self, image_path: Path
    ) -> tuple[int | None, int | None, str]:
        if image_path.suffix.lower() == ".svg":
            width, height = self._read_svg_dimensions(image_path)
            return width, height, "unknown"
        try:
            with Image.open(image_path) as image:
                width, height = image.size
                has_alpha = (
                    image.mode in {"RGBA", "LA"}
                    or "transparency" in image.info
                    or (getattr(image, "mode", "") == "P" and "transparency" in image.info)
                )
                return int(width), int(height), "yes" if has_alpha else "no"
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            return None, None, "unknown"

    @staticmethod
    def _read_svg_dimensions(image_path: Path) -> tuple[int | None, int | None]:
        try:
            root = ElementTree.parse(image_path).getroot()
        except (ElementTree.ParseError, OSError):
            return None, None
        width = StyleClassifier._parse_numeric_dimension(root.attrib.get("width"))
        height = StyleClassifier._parse_numeric_dimension(root.attrib.get("height"))
        if width is not None and height is not None:
            return width, height
        viewbox = root.attrib.get("viewBox")
        if viewbox:
            parts = [part for part in viewbox.replace(",", " ").split() if part]
            if len(parts) == 4:
                try:
                    return int(float(parts[2])), int(float(parts[3]))
                except ValueError:
                    return None, None
        return None, None

    @staticmethod
    def _parse_numeric_dimension(value: str | None) -> int | None:
        if not value:
            return None
        matched = re.match(r"^\s*([0-9]*\.?[0-9]+)", value)
        if not matched:
            return None
        return int(float(matched.group(1)))
