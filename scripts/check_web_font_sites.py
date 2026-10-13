"""Manually check public font sources without retaining downloaded font binaries."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.safe_remote_fetcher import open_safe_remote
from app.services.web_font_service import (
    MAX_PAGE_BYTES, MAX_STYLESHEETS, MAX_WEB_FONTS, MAX_WEB_FONT_BYTES,
    WEB_FONT_PREVIEW_TEXT, PageFontHTMLParser, css_font_references, download_web_fonts,
)

DEFAULT_URLS = (
    "https://rsms.me/",
    "https://simonwillison.net/",
    "https://www.kyoto-u.ac.jp/ja",
    "https://static.giantaccel.com/",
    "https://static.giantaccel.com/fonts/noto-sans-sc/result.css",
)


def declarations(text: str, kind: str, url: str) -> dict:
    """Summarize fetched static declarations, never infer computed styles."""
    if kind == "text/css":
        css, base, stylesheets, html_references = text, url, [], []
    else:
        parser = PageFontHTMLParser(url)
        parser.feed(text)
        css, base, stylesheets = "\n".join(parser.styles), parser.base, parser.stylesheets
        html_references = parser.references
    references, imports = css_font_references(css, base)
    references = html_references + references
    stacks = list(dict.fromkeys(re.findall(r"font-family\s*:\s*([^;}]+)", css, re.I)))
    return {
        "stylesheets": stylesheets,
        "imports": imports,
        "font_family_declarations_first_24": stacks[:24],
        "font_face_token_count": len(re.findall(r"@font-face\b", css, re.I)),
        "unicode_range_token_count": len(re.findall(r"unicode-range\s*:", css, re.I)),
        "references_with_product_parser_limits": [
            {"hint": reference.hint, "sources": [
                {"url": source, "extension_hint": extension}
                for source, extension in reference.sources
            ]} for reference in references
        ],
    }


class ObservedResponse:
    def __init__(self, response, event: dict, started: float):
        self.response, self.event, self.started = response, event, started
        self.headers = response.headers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc is not None:
            self.event["error"] = {"type": type(exc).__name__, "message": str(exc)}
        try:
            return self.response.__exit__(exc_type, exc, traceback)
        finally:
            self.event["elapsed_seconds"] = round(time.monotonic() - self.started, 3)

    def iter_bytes(self):
        digest, body, size = hashlib.sha256(), bytearray(), 0
        kind = self.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        is_text = kind in {"text/html", "application/xhtml+xml", "text/css"}
        complete = False
        try:
            for chunk in self.response.iter_bytes():
                digest.update(chunk)
                size += len(chunk)
                if is_text and len(body) <= MAX_PAGE_BYTES:
                    body.extend(chunk[:MAX_PAGE_BYTES + 1 - len(body)])
                yield chunk
            complete = True
        except Exception as exc:
            self.event["stream_error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            self.event.update(bytes_observed=size, body_complete=complete, sha256=digest.hexdigest())
            if is_text and complete and len(body) <= MAX_PAGE_BYTES:
                charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", self.headers.get("content-type", ""), re.I)
                try:
                    text = body.decode(charset[1] if charset else "utf-8", errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
                self.event["static_declarations"] = declarations(text, kind, self.event["url"])


def check_site(url: str, args) -> dict:
    started = time.monotonic()
    deadline = started + args.site_seconds
    requests: list[dict] = []
    stopped = lambda: time.monotonic() >= deadline

    def observe(request_url: str, *, timeout: float, should_stop):
        event = {"url": request_url}
        requests.append(event)
        request_started = time.monotonic()
        try:
            response = open_safe_remote(request_url, timeout=min(timeout, max(.1, deadline - time.monotonic())),
                                        should_stop=should_stop)
            event.update(status=response.status, content_type=response.headers.get("content-type", ""),
                         content_encoding=response.headers.get("content-encoding", "identity"))
            return ObservedResponse(response, event, request_started)
        except Exception as exc:
            event["error"] = {"type": type(exc).__name__, "message": str(exc)}
            event["elapsed_seconds"] = round(time.monotonic() - request_started, 3)
            raise

    result = {"url": url, "checked_at": datetime.now(timezone.utc).isoformat(), "requests": requests,
              "preview_sample": WEB_FONT_PREVIEW_TEXT, "candidates": [], "truncated": False,
              "failed_resources": 0}
    with tempfile.TemporaryDirectory(prefix="haypile-web-font-check-") as temporary:
        temporary_path = Path(temporary)
        try:
            candidates, truncated, failed = download_web_fonts(
                url, temporary_path / "incoming", timeout=args.request_seconds, should_stop=stopped,
                opener=observe, max_fonts=args.max_fonts, max_bytes=args.max_bytes,
            )
            result.update(truncated=truncated, failed_resources=failed)
            for candidate in candidates:
                sources = [event["url"] for event in requests
                           if event.get("body_complete") and event.get("sha256") == candidate.sha256]
                result["candidates"].append({
                    "sha256": candidate.sha256, "source_urls": sources,
                    "extension": candidate.path.suffix, "bytes": candidate.path.stat().st_size,
                    "origin": candidate.origin, "metadata": candidate.metadata,
                    "declaration_hint": candidate.hint,
                    "declared_unicode_range_hint": candidate.hint[candidate.hint.lower().find("u+"):] if "u+" in candidate.hint.lower() else "",
                    "missing_sample": candidate.missing, "preview_dimensions": [candidate.width, candidate.height],
                })
            result["status"] = "candidates_found" if candidates else "no_static_candidates"
        except Exception as exc:
            result.update(status="error", error={"type": type(exc).__name__, "message": str(exc)})
        result["deadline_reached"] = stopped()
        if result["deadline_reached"]:
            result["status"] = "cancelled"
    result["temporary_directory_removed"] = not temporary_path.exists()
    result["parsed_font_reference_count"] = sum(
        len(event.get("static_declarations", {}).get("references_with_product_parser_limits", []))
        for event in requests
    )
    if result["status"] == "no_static_candidates":
        if result["parsed_font_reference_count"]:
            result["status"] = "declared_but_not_collected"
        elif result["failed_resources"]:
            result["status"] = "unresolved_resource_failures"
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="Public HTML or CSS URLs; defaults to five representative inputs.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-fonts", type=int, default=6)
    parser.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--site-seconds", type=float, default=45)
    parser.add_argument("--request-seconds", type=float, default=8)
    args = parser.parse_args()
    if min(args.max_fonts, args.max_bytes, args.site_seconds, args.request_seconds) <= 0:
        parser.error("Limits must be positive.")
    args.out.mkdir(parents=True, exist_ok=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
    limits = {"max_fonts": min(args.max_fonts, MAX_WEB_FONTS), "max_bytes": min(args.max_bytes, MAX_WEB_FONT_BYTES),
              "site_seconds": args.site_seconds, "request_seconds": args.request_seconds,
              "product_max_page_bytes": MAX_PAGE_BYTES, "product_max_stylesheets": MAX_STYLESHEETS,
              "product_max_font_url_attempts": MAX_WEB_FONTS * 2}
    report = {"revision": revision, "checked_at": datetime.now(timezone.utc).isoformat(), "limits": limits,
              "service_sha256": {name: hashlib.sha256((ROOT / "app/services" / name).read_bytes()).hexdigest()
                                 for name in ("web_font_service.py", "safe_remote_fetcher.py", "font_service.py")},
              "boundary": "Original public-address-only fetcher; no redirects, proxies, login, or JavaScript. Site cancellation is cooperative, including the product's socket watchdog; DNS is checked when platform resolution returns.",
              "interpretation": "Candidates are downloadable static declarations. Counts are not accuracy, completeness, or proof of the font rendered for any text.",
              "sites": []}
    for index, url in enumerate(args.urls or DEFAULT_URLS, 1):
        result = check_site(url, args)
        result["limits"] = limits
        filename = f"{index:02d}-{urlsplit(url).hostname or 'invalid-url'}.json"
        (args.out / filename).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["sites"].append({"url": url, "evidence_file": filename, "status": result["status"],
                                "candidates": len(result["candidates"]), "truncated": result["truncated"],
                                "parsed_font_reference_count": result["parsed_font_reference_count"],
                                "failed_resources": result["failed_resources"], "elapsed_seconds": result["elapsed_seconds"],
                                "temporary_directory_removed": result["temporary_directory_removed"]})
        print(f"{url}: {result['status']}, {len(result['candidates'])} candidates, "
              f"{result['failed_resources']} failed resources, truncated={result['truncated']}, "
              f"{result['elapsed_seconds']}s", flush=True)
    (args.out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(any(site["status"] in {"error", "cancelled", "declared_but_not_collected", "unresolved_resource_failures"}
                   or not site["temporary_directory_removed"] for site in report["sites"]))


if __name__ == "__main__":
    raise SystemExit(main())
