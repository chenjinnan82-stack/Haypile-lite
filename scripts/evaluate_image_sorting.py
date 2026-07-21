#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.services.ai_provider import AIProviderConfig, api_authority
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


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [record for record in records if record["auto_ready"]]
    correct = sum(bool(record["correct"]) for record in ready)
    accuracy = correct / len(ready) if ready else 0.0
    coverage = len(ready) / len(records) if records else 0.0
    gate = 80 <= len(records) <= 120 and len(ready) >= 30 and accuracy >= 0.90
    return {
        "sample_count": len(records),
        "auto_ready_count": len(ready),
        "auto_ready_correct": correct,
        "auto_ready_accuracy": round(accuracy, 4),
        "coverage": round(coverage, 4),
        "release_gate_passed": gate,
    }


def load_samples(dataset: Path, labels_path: Path) -> list[tuple[Path, str, str]]:
    root = dataset.resolve(strict=True)
    payload = json.loads(labels_path.read_text(encoding="utf-8"))
    raw_samples = payload.get("samples") if isinstance(payload, dict) else None
    if not isinstance(raw_samples, list):
        raise ValueError("labels must contain a samples list")

    samples: list[tuple[Path, str, str]] = []
    for item in raw_samples:
        if not isinstance(item, dict):
            raise ValueError("every sample must be an object")
        relative = str(item.get("file") or "").strip()
        expected = str(item.get("role") or "").strip().lower()
        if not relative or expected not in IMAGE_ROLES:
            raise ValueError("every sample needs a relative file and supported role")
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise ValueError(f"sample is not a file: {relative}")
        samples.append((path, relative.replace("\\", "/"), expected))
    return samples


async def evaluate(
    samples: list[tuple[Path, str, str]],
    provider: AIProviderConfig,
) -> list[dict[str, Any]]:
    classifier = StyleClassifier(provider)
    records: list[dict[str, Any]] = []
    for path, relative, expected in samples:
        result = await classifier.classify_image(path)
        auto_ready = StyleClassifier.is_auto_ready(result)
        records.append(
            {
                "file": relative,
                "expected_role": expected,
                "predicted_role": result.role,
                "role_confidence": result.role_confidence,
                "quality": result.quality,
                "quality_reason": result.quality_reason,
                "auto_ready": auto_ready,
                "correct": result.role == expected,
            }
        )
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate Haypile image sorting without ingesting files.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--mode", choices=("local", "api"), default="local")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--output", type=Path)
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
    records = asyncio.run(evaluate(load_samples(args.dataset, args.labels), provider))
    report = {"summary": summarize(records), "samples": records}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 1 if args.enforce_release_gate and not report["summary"]["release_gate_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
