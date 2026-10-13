#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.services.ai_provider import AIProviderConfig, api_authority
from app.services.media_validator import MediaValidationError, validate_raster
from app.services.style_classifier import StyleClassifier


IMAGE_ROLES = {
    "main_background",
    "hero_image",
    "logo",
    "icon",
    "content_image",
    "texture",
    "unknown",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    # Older reports omitted source; keep their existing gate calculation valid.
    model_results = [
        record
        for record in records
        if record.get("source", "model") == "model" and not record.get("error_type")
    ]
    ready = [record for record in model_results if record["auto_ready"]]
    correct = sum(bool(record["correct"]) for record in ready)
    accuracy = correct / len(ready) if ready else 0.0
    coverage = len(ready) / len(records) if records else 0.0
    gate = 80 <= len(records) <= 120 and len(ready) >= 30 and accuracy >= 0.90
    elapsed = sorted(
        float(record["elapsed_seconds"])
        for record in records
        if isinstance(record.get("elapsed_seconds"), (int, float))
        and math.isfinite(record["elapsed_seconds"])
        and record["elapsed_seconds"] >= 0
    )
    # P95 uses nearest rank: sorted_times[ceil(0.95 * count) - 1].
    p95 = elapsed[math.ceil(0.95 * len(elapsed)) - 1] if elapsed else 0.0
    return {
        "sample_count": len(records),
        "auto_ready_count": len(ready),
        "auto_ready_correct": correct,
        "auto_ready_accuracy": round(accuracy, 4),
        "coverage": round(coverage, 4),
        "release_gate_passed": gate,
        "failure_count": len(records) - len(model_results),
        "actual_model_result_count": len(model_results),
        "timed_sample_count": len(elapsed),
        "elapsed_seconds_median": round(statistics.median(elapsed), 6) if elapsed else 0.0,
        "elapsed_seconds_p95": round(p95, 6),
        "elapsed_seconds_max": round(max(elapsed), 6) if elapsed else 0.0,
        "p95_method": "nearest_rank",
    }


def load_samples(dataset: Path, labels_path: Path) -> list[tuple[Path, str, str]]:
    root = dataset.resolve(strict=True)
    payload = json.loads(labels_path.read_text(encoding="utf-8"))
    raw_samples = payload.get("samples") if isinstance(payload, dict) else None
    if not isinstance(raw_samples, list):
        raise ValueError("labels must contain a samples list")

    samples: list[tuple[Path, str, str]] = []
    seen_paths: set[Path] = set()
    seen_content: dict[str, str] = {}
    for item in raw_samples:
        if not isinstance(item, dict):
            raise ValueError("every sample must be an object")
        relative = str(item.get("file") or "").strip()
        expected = str(item.get("role") or "").strip().lower()
        if not relative or Path(relative).is_absolute() or expected not in IMAGE_ROLES:
            raise ValueError("every sample needs a relative file and supported role")
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise ValueError(f"sample is not a file: {relative}")
        if path in seen_paths:
            raise ValueError(f"duplicate sample file: {relative}")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError(f"unsupported evaluation image: {relative}")
        try:
            validate_raster(path)
        except MediaValidationError as exc:
            raise ValueError(f"invalid evaluation image: {relative}: {exc}") from exc
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest in seen_content:
            raise ValueError(
                f"duplicate sample content: {relative} matches {seen_content[digest]}"
            )
        seen_paths.add(path)
        seen_content[digest] = relative
        samples.append((path, relative.replace("\\", "/"), expected))
    return samples


async def evaluate(
    samples: list[tuple[Path, str, str]],
    provider: AIProviderConfig,
    timeout_seconds: float | None = None,
) -> list[dict[str, Any]]:
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be finite and greater than zero")
    classifier = StyleClassifier(provider)
    if timeout_seconds is not None:
        # This diagnostic override must never mutate the cached application settings.
        classifier.timeout_seconds = timeout_seconds
    records: list[dict[str, Any]] = []
    for path, relative, expected in samples:
        started = time.perf_counter()
        try:
            result = await classifier.classify_image(path)
            record = {
                "file": relative,
                "expected_role": expected,
                "predicted_role": result.role,
                "role_confidence": result.role_confidence,
                "quality": result.quality,
                "quality_reason": result.quality_reason,
                "source": result.source,
                "reason": result.reason,
                "error_type": "",
                "auto_ready": (
                    result.source == "model" and StyleClassifier.is_auto_ready(result)
                ),
                "correct": result.source == "model" and result.role == expected,
            }
        except Exception as exc:
            # Exception messages can contain request URLs or credentials. Record only
            # the type, while preserving the failed image and continuing the run.
            record = {
                "file": relative,
                "expected_role": expected,
                "predicted_role": "unknown",
                "role_confidence": 0.0,
                "quality": "unknown",
                "quality_reason": "classification_exception",
                "source": "exception",
                "reason": "classification_exception",
                "error_type": type(exc).__name__,
                "auto_ready": False,
                "correct": False,
            }
        record["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        records.append(record)
        print(f"[{len(records)}/{len(samples)}] {relative}: {record['source']} "
              f"{record['elapsed_seconds']:.2f}s", file=sys.stderr, flush=True)
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate Haypile image sorting without ingesting files.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--mode", choices=("local", "api"), default="local")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="Override the timeout for this evaluation only; application settings are unchanged.",
    )
    parser.add_argument("--enforce-release-gate", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    api_key = os.environ.get("HAYPILE_EVAL_API_KEY", "") if args.mode == "api" else ""
    if args.mode == "api" and not api_key:
        raise SystemExit("HAYPILE_EVAL_API_KEY is required for API evaluation")
    base_url = args.base_url or settings.VISION_CLASSIFIER_BASE_URL
    provider = AIProviderConfig(
        mode=args.mode,
        base_url=base_url,
        model=args.model or settings.VISION_CLASSIFIER_MODEL,
        api_key=api_key,
        authorized_host=api_authority(base_url) if args.mode == "api" else "",
    )
    records = asyncio.run(
        evaluate(load_samples(args.dataset, args.labels), provider, args.timeout_seconds)
    )
    configured_timeout = float(settings.VISION_CLASSIFIER_TIMEOUT_SECONDS)
    report = {
        "provider": {
            "mode": provider.mode,
            "model": provider.model,
            "transport": "openai" if provider.mode == "api" else "ollama",
            "configured_timeout_seconds": configured_timeout,
            "timeout_seconds": (
                args.timeout_seconds if args.timeout_seconds is not None else configured_timeout
            ),
        },
        "summary": summarize(records),
        "samples": records,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"provider": report["provider"], "summary": report["summary"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.enforce_release_gate and not report["summary"]["release_gate_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
