"""Bounded static-page image candidates; original files stay unconfirmed."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from html.parser import HTMLParser
import math
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from app.services.asset_provenance import provenance_path_for, public_origin_url, read_asset_provenance, write_asset_provenance
from app.services.media_validator import validate_media
from app.services.safe_remote_fetcher import download_remote_media, open_safe_remote
from app.services.web_font_service import read_web_text

MAX_WEB_IMAGES = 12
MAX_WEB_IMAGE_BYTES = 40 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
_SPACE = " \t\r\n\f"


@dataclass(frozen=True)
class ImageReference:
    sources: tuple[str, ...]
    hint: str
    label: str = ""


@dataclass(frozen=True)
class WebImageCandidate:
    path: Path
    sha256: str
    metadata: dict
    origin: str
    hint: str
    label: str
    pixels: bytes = b""
    width: int = 0
    height: int = 0


def _url(base: str, value: str) -> str:
    try:
        result = urldefrag(urljoin(base, value.strip()))[0]
        return result if urlsplit(result).scheme.lower() in {"http", "https"} else ""
    except ValueError:
        return ""


def srcset_urls(value: str) -> list[str]:
    # ponytail: prefer largest declared w/x candidates, not browser viewport selection.
    if len(value) > 16384:
        return []
    position = 0
    candidates = []
    while position < len(value) and len(candidates) < 32:
        while position < len(value) and value[position] in _SPACE + ",":
            position += 1
        start = position
        while position < len(value) and value[position] not in _SPACE:
            position += 1
        url = value[start:position]
        descriptor = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            start = position
            while position < len(value) and value[position] != ",":
                position += 1
            descriptor = value[start:position].strip(_SPACE)
        rank = 1.0
        width = False
        if descriptor:
            if re.fullmatch(r"[0-9]{1,10}w", descriptor):
                rank, width = int(descriptor[:-1]), True
            elif re.fullmatch(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?x", descriptor):
                rank = float(descriptor[:-1])
            else:
                continue
        if url and math.isfinite(rank) and rank > 0:
            candidates.append((width, rank, url))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [url for _, _, url in candidates]


class PageImageHTMLParser(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = page_url
        self.references: list[ImageReference] = []
        self.limited = False
        self._base_seen = False
        self._ignored: list[str] = []
        self._picture: list[str] | None = None

    def _add(self, sources: list[str], hint: str, label: str = "") -> None:
        sources = list(dict.fromkeys(source for source in sources if source and _url(self.base, source)))[:4]
        if not sources:
            return
        if len(self.references) >= MAX_WEB_IMAGES * 4:
            self.limited = True
        else:
            self.references.append(ImageReference(tuple(sources), hint, label[:200]))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template"}:
            self._ignored.append(tag)
        if self._ignored:
            return
        values = {key: value for key, value in attrs if value}
        if tag == "base" and not self._base_seen and any(key == "href" for key, _ in attrs):
            self.base = _url(self.base, values.get("href", "")) or self.base
            self._base_seen = True
        if tag == "meta" and (values.get("property") or values.get("name", "")).lower() in {
            "og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src",
        }:
            self._add([values.get("content", "")], "cover")
        if tag == "link":
            rel = values.get("rel", "").lower().split()
            if "image_src" in rel:
                self._add([values.get("href", "")], "cover")
            elif "icon" in rel or "apple-touch-icon" in rel:
                self._add([values.get("href", "")], "icon")
        if tag == "picture":
            self._picture = []
        if tag == "source" and self._picture is not None:
            if len(self._picture) < 4:
                self._picture.extend(srcset_urls(values.get("srcset", ""))[:2])
        if tag == "img":
            if all(re.fullmatch(r"[0-2]", values.get(key, "")) for key in ("width", "height")):
                return
            sources = srcset_urls(values.get("data-srcset") or values.get("srcset", ""))[:2]
            sources += [values.get("data-src", ""), values.get("src", "")]
            # Prefer the img's fallback to avoid choosing a picture media query blindly.
            sources += self._picture or []
            self._add(sources, "image", values.get("alt", ""))

    def handle_endtag(self, tag: str) -> None:
        if self._ignored and self._ignored[-1] == tag:
            self._ignored.pop()
        if tag == "picture":
            self._picture = None


def download_web_images(
    page_url: str, incoming_dir: Path, *, timeout: float = 15,
    should_stop: Callable[[], bool] = lambda: False, opener: Callable = open_safe_remote,
    max_bytes: int = MAX_WEB_IMAGE_BYTES, max_images: int = MAX_WEB_IMAGES,
    document: tuple[str, str] | None = None,
) -> tuple[list[WebImageCandidate], bool, int]:
    text, content_type = document if document is not None else read_web_text(page_url, opener=opener, timeout=timeout, should_stop=should_stop)
    if content_type == "text/css":
        return [], False, 0
    parser = PageImageHTMLParser(page_url)
    parser.feed(text)
    parser.close()
    order = {"cover": 0, "image": 1, "icon": 2}
    references = sorted(parser.references, key=lambda item: order[item.hint])
    candidates: list[WebImageCandidate] = []
    seen: set[str] = set()
    successful: set[str] = set()
    hashes: set[str] = set()
    limited, failed, used_bytes = parser.limited, 0, 0
    incoming_dir.mkdir(parents=True, exist_ok=True)
    try:
        for reference in references:
            if should_stop():
                break
            if len(candidates) >= min(max_images, MAX_WEB_IMAGES) or len(seen) >= MAX_WEB_IMAGES * 2:
                limited = True
                break
            for value in reference.sources:
                url = _url(parser.base, value)
                if not url:
                    continue
                if url in successful:
                    break
                if url in seen:
                    continue
                remaining = min(max_bytes, MAX_WEB_IMAGE_BYTES) - used_bytes
                if remaining <= 0 or len(seen) >= MAX_WEB_IMAGES * 2:
                    limited = True
                    break
                if should_stop():
                    break
                seen.add(url)
                path = None
                try:
                    path, _reason = download_remote_media(url, incoming_dir, len(seen), max_bytes=min(remaining, MAX_IMAGE_BYTES),
                        timeout=timeout, should_stop=should_stop, opener=opener, image_only=True)
                    if path is None:
                        if not should_stop():
                            failed += 1
                        continue
                    used_bytes += path.stat().st_size
                    validation = validate_media(path)
                    if validation.kind != "image":
                        raise ValueError("unsupported_image")
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    successful.add(url)
                    if digest in hashes:
                        break
                    provenance = read_asset_provenance(path)
                    provenance["origin_url"] = public_origin_url(page_url)
                    write_asset_provenance(path, provenance)
                    candidates.append(WebImageCandidate(path, digest, asdict(validation), public_origin_url(page_url), reference.hint, reference.label))
                    hashes.add(digest)
                    path = None
                    break
                except (httpx.HTTPError, OSError, ValueError):
                    failed += 1
                finally:
                    if path is not None:
                        path.unlink(missing_ok=True)
                        provenance_path_for(path).unlink(missing_ok=True)
    except BaseException:
        for candidate in candidates:
            candidate.path.unlink(missing_ok=True)
            provenance_path_for(candidate.path).unlink(missing_ok=True)
        raise
    if should_stop():
        for candidate in candidates:
            candidate.path.unlink(missing_ok=True)
            provenance_path_for(candidate.path).unlink(missing_ok=True)
        candidates.clear()
    return candidates, limited, failed
