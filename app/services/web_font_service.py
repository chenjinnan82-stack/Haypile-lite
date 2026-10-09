"""Bounded discovery of downloadable fonts in a page and its static stylesheets."""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
from pathlib import Path
import re
from typing import Callable
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import httpx
from app.services.asset_provenance import provenance_path_for, public_origin_url, read_asset_provenance, write_asset_provenance
from app.services.font_service import MAX_FONT_BYTES, read_font_metadata, render_font_preview
from app.services.media_types import FONT_CONTENT_TYPE_EXTENSIONS, SUPPORTED_FONT_EXTENSIONS
from app.services.safe_remote_fetcher import SafeFetchError, download_remote_media, open_safe_remote

WEB_FONT_PREVIEW_TEXT = "冬藏 Aa0123"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_STYLESHEETS = 8
MAX_WEB_FONTS = 20
MAX_WEB_FONT_BYTES = 100 * 1024 * 1024
_CSS_TOKEN = re.compile(
    r'(?P<comment>/\*)|(?P<string>[\'\"])|(?P<url>url\()|(?P<at>@[-\w\\]+)|(?P<brace>[{};])', re.IGNORECASE,
)


@dataclass(frozen=True)
class FontReference:
    sources: tuple[tuple[str, str], ...]
    hint: str = ""


@dataclass(frozen=True)
class WebFontCandidate:
    path: Path
    sha256: str
    metadata: dict
    pixels: bytes
    width: int
    height: int
    missing: str
    origin: str
    hint: str


def _url(base: str, value: str) -> str:
    try:
        result = urldefrag(urljoin(base, value.strip()))[0]
        return result if urlsplit(result).scheme.lower() in {"http", "https"} else ""
    except ValueError:
        return ""


def _css_unescape(value: str) -> str:
    def replace(match: re.Match) -> str:
        escape = match.group(1)
        if escape[0] in "\r\n\f":
            return ""
        number = escape.strip()
        if re.fullmatch(r"[0-9a-fA-F]{1,6}", number):
            code = int(number, 16)
            return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "\ufffd"
        return escape
    return re.sub(r"\\([0-9a-fA-F]{1,6}\s?|\r\n|[\s\S])", replace, value)


def _css_tokens(css: str):
    """Advance once through comments, quoted strings and URL arguments, including malformed CSS."""
    position = 0
    while token := _CSS_TOKEN.search(css, position):
        kind, value = token.lastgroup, token.group()
        position = token.end()
        if kind == "comment":
            end = css.find("*/", position)
            if end < 0:
                break
            position = end + 2
            continue
        if kind in {"url", "string"}:
            while kind == "url" and position < len(css) and css[position].isspace():
                position += 1
            quoted = kind == "string" or (position < len(css) and css[position] in "\"'")
            closer = value if kind == "string" else css[position] if quoted else ")"
            if kind == "url" and quoted:
                position += 1
            start = position
            while position < len(css) and css[position] != closer:
                position += 2 if css[position] == "\\" else 1
            if position >= len(css):
                break
            value = css[start:position]
            position += 1
            if kind == "url" and quoted:
                while position < len(css) and css[position].isspace():
                    position += 1
                if position >= len(css) or css[position] != ")":
                    continue
                position += 1
        yield kind, value, token.start(), position


def css_font_references(css: str, base: str) -> tuple[list[FontReference], list[str]]:
    references: list[FontReference] = []
    imports: list[str] = []
    importing = waiting = False
    depth = 0
    start = 0
    sources: list[tuple[str, str]] = []
    # ponytail: scan static font-face / import rules; script-created styles need a browser extension.
    for kind, value, begin, end in _css_tokens(css):
        at = _css_unescape(value).lower() if kind == "at" else ""
        brace = value if kind == "brace" else ""
        if at == "@font-face" and not depth:
            waiting = True
        elif at == "@import" and not depth:
            importing = True
        elif brace == "{" and waiting:
            waiting = False
            depth, start, sources = 1, end, []
        elif depth and brace == "{":
            depth += 1
        elif depth and brace == "}":
            depth -= 1
            if not depth and sources:
                body = css[start:begin]
                hint = "; ".join(re.findall(r"(?:font-family|font-weight|font-style|unicode-range)\s*:\s*([^;{}]+)", body, re.I))[:256]
                references.append(FontReference(tuple(sources), hint))
        elif kind == "url" or (importing and kind == "string"):
            url = _url(base, _css_unescape(value))
            if importing:
                if url:
                    imports.append(url)
                importing = False
            elif depth and url:
                extension = Path(unquote(urlsplit(url).path)).suffix.lower()
                if extension not in SUPPORTED_FONT_EXTENSIONS:
                    following = css[end:end + 100]
                    format_match = re.match(r"\s*format\(\s*['\"]?(woff2|woff|truetype|opentype)['\"]?\s*\)", following, re.I)
                    extension = {"truetype": ".ttf", "opentype": ".otf"}.get(format_match[1].lower(), "." + format_match[1].lower()) if format_match else ""
                sources.append((url, extension))
        elif brace == ";":
            importing = waiting = False
        if len(references) >= MAX_WEB_FONTS * 4 or len(imports) >= MAX_STYLESHEETS * 4:
            break
    return references, imports


class PageFontHTMLParser(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = page_url
        self.references: list[FontReference] = []
        self.stylesheets: list[str] = []
        self.styles: list[str] = []
        self._style = False
        self._base_seen = False
        self._template_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "template":
            self._template_depth += 1
        if self._template_depth:
            return
        values = {key.lower(): value for key, value in attrs if value}
        if tag == "base" and not self._base_seen and any(key == "href" for key, _ in attrs):
            self.base = _url(self.base, values.get("href", "")) or self.base
            self._base_seen = True
        if tag == "style":
            self._style = True
        href = values.get("href", "")
        if not href or tag not in {"a", "link"}:
            return
        url = _url(self.base, href)
        if not url:
            return
        rel = values.get("rel", "").lower().split()
        if tag == "link" and "stylesheet" in rel:
            if len(self.stylesheets) < MAX_STYLESHEETS * 4:
                self.stylesheets.append(url)
            return
        extension = Path(unquote(urlsplit(url).path)).suffix.lower()
        if extension not in SUPPORTED_FONT_EXTENSIONS:
            extension = Path(values.get("download", "")).suffix.lower()
        content_type = values.get("type", "").lower()
        if extension not in SUPPORTED_FONT_EXTENSIONS:
            extension = FONT_CONTENT_TYPE_EXTENSIONS.get(content_type, "")
        if extension in SUPPORTED_FONT_EXTENSIONS or values.get("as", "").lower() == "font" or content_type.startswith("font/"):
            if len(self.references) < MAX_WEB_FONTS * 4:
                self.references.append(FontReference(((url, extension),)))

    def handle_data(self, data: str) -> None:
        if self._style and not self._template_depth:
            self.styles.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "template" and self._template_depth:
            self._template_depth -= 1
        if tag == "style":
            self._style = False


def read_web_text(url: str, *, opener: Callable, timeout: float, should_stop: Callable) -> tuple[str, str]:
    with opener(url, timeout=timeout) as response:
        raw_type = response.headers.get("content-type", "")
        content_type = raw_type.split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml", "text/css"}:
            raise SafeFetchError("unsupported_web_page")
        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise SafeFetchError("encoded_web_page")
        if int(response.headers.get("content-length", "0") or "0") > MAX_PAGE_BYTES:
            raise SafeFetchError("web_page_size_limit")
        body = bytearray()
        for chunk in response.iter_bytes():
            if should_stop():
                raise SafeFetchError("interrupted")
            if len(body) + len(chunk) > MAX_PAGE_BYTES:
                raise SafeFetchError("web_page_size_limit")
            body.extend(chunk)
        encoding = re.search(r"charset\s*=\s*[\"']?([\w-]+)", raw_type, re.I)
        try:
            text = body.decode(encoding[1] if encoding else "utf-8", errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return text, content_type


def download_web_fonts(
    page_url: str, incoming_dir: Path, *, timeout: float = 15,
    should_stop: Callable[[], bool] = lambda: False, opener: Callable = open_safe_remote,
    max_bytes: int = MAX_WEB_FONT_BYTES,
    max_fonts: int = MAX_WEB_FONTS,
    document: tuple[str, str] | None = None,
) -> tuple[list[WebFontCandidate], bool, int]:
    """Every page, stylesheet and font uses the same public-address-only fetcher."""
    if max_fonts <= 0 or max_bytes <= 0:
        return [], True, 0
    text, content_type = document if document is not None else read_web_text(page_url, opener=opener, timeout=timeout, should_stop=should_stop)
    if content_type == "text/css":
        references, stylesheets = css_font_references(text, page_url)
    else:
        parser = PageFontHTMLParser(page_url)
        parser.feed(text)
        references, stylesheets = parser.references, parser.stylesheets
        inline, imports = css_font_references("\n".join(parser.styles), parser.base)
        references.extend(inline)
        stylesheets.extend(imports)
    seen_stylesheets = {page_url}
    failed = 0
    truncated = False
    for url in stylesheets:
        if should_stop():
            break
        if url in seen_stylesheets:
            continue
        if len(seen_stylesheets) > MAX_STYLESHEETS:
            truncated = True
            break
        seen_stylesheets.add(url)
        try:
            css, kind = read_web_text(url, opener=opener, timeout=timeout, should_stop=should_stop)
            if kind != "text/css":
                failed += 1
                continue
            found, imports = css_font_references(css, url)
            references.extend(found)
            stylesheets.extend(imports)
        except (httpx.HTTPError, OSError, ValueError):
            failed += 1
    candidates: list[WebFontCandidate] = []
    seen_urls: set[str] = set()
    hashes: set[str] = set()
    successful_urls: set[str] = set()
    used_bytes = 0
    incoming_dir.mkdir(parents=True, exist_ok=True)
    try:
        for reference in references:
            if should_stop():
                break
            if len(candidates) >= min(max_fonts, MAX_WEB_FONTS) or len(seen_urls) >= MAX_WEB_FONTS * 2:
                truncated = True
                break
            for url, extension in reference.sources:
                if url in seen_urls:
                    if url in successful_urls:
                        break  # The same face's alternate formats are not separate candidates.
                    continue
                if len(seen_urls) >= MAX_WEB_FONTS * 2 or should_stop():
                    truncated = len(seen_urls) >= MAX_WEB_FONTS * 2
                    break
                seen_urls.add(url)
                remaining = min(max_bytes, MAX_WEB_FONT_BYTES) - used_bytes
                if remaining <= 0:
                    truncated = True
                    break
                try:
                    path, reason = download_remote_media(url, incoming_dir, len(seen_urls), max_bytes=min(remaining, MAX_FONT_BYTES),
                        timeout=timeout, should_stop=should_stop, opener=opener, font_extension=extension, font_only=True)
                except (httpx.HTTPError, OSError, ValueError):
                    failed += 1
                    continue
                if path is None:
                    if reason != "interrupted":
                        failed += 1
                    continue
                try:
                    used_bytes += path.stat().st_size
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    if digest in hashes:
                        successful_urls.add(url)
                        break
                    metadata = read_font_metadata(path)
                    # 2× pixels; the desktop card sets the display density.
                    image, missing = render_font_preview(path, WEB_FONT_PREVIEW_TEXT, 64, width=508)
                    try:
                        pixels, width, height = image.tobytes(), image.width, image.height
                    finally:
                        image.close()
                    provenance = read_asset_provenance(path)
                    provenance["origin_url"] = public_origin_url(page_url)
                    write_asset_provenance(path, provenance)
                    candidates.append(WebFontCandidate(path, digest, metadata, pixels, width, height, missing, public_origin_url(page_url), reference.hint))
                    hashes.add(digest)
                    successful_urls.add(url)
                    path = None  # Ownership passes to the caller only after validation and preview.
                except (OSError, ValueError):
                    failed += 1
                    continue
                finally:
                    if path is not None:
                        path.unlink(missing_ok=True)
                        provenance_path_for(path).unlink(missing_ok=True)
                break
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
    candidates.sort(key=lambda candidate: len(candidate.missing))
    return candidates, truncated, failed
