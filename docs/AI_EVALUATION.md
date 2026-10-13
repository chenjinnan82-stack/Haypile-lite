# Image sorting evaluation

The v0.3 release gate uses 80–120 owned or licensed images without importing
them into Haypile. Keep the dataset outside the repository or under the ignored
`private-eval/` or `build/` directory. Freeze human labels before predictions;
do not relabel failures or omit difficult images to pass the gate. Avoid
repeated content, even under different filenames.

Create a labels file:

```json
{
  "samples": [
    {"file": "hero/landing.webp", "role": "hero_image"},
    {"file": "brand/mark.png", "role": "logo"}
  ]
}
```

Run the local model evaluation:

```bash
python3 scripts/evaluate_image_sorting.py \
  private-eval/images private-eval/labels.json \
  --output private-eval/report.json \
  --enforce-release-gate
```

For an authorized OpenAI-compatible service, pass `--mode api`, `--base-url`,
and `--model`; provide the key only through `HAYPILE_EVAL_API_KEY`. The report
contains relative sample names and model results, never image bytes, API keys,
or absolute paths. It records the selected model, effective inference budget,
per-sample latency, fallback/exception counts, and median/P95/maximum latency.
P95 uses the nearest-rank method. One failed request does not stop the dataset;
failures count against coverage and never count as correct automatic results.

`--timeout-seconds 45` changes only this evaluation run, leaving application
settings unchanged. Progress goes to stderr; the JSON report is saved at the
end. Invalid or mismatched image formats and duplicate files/content are
rejected before inference.

The release gate passes only when there are 80–120 labeled samples, at least 30
automatic-ready results, and at least 90% role accuracy among those automatic
results. Coverage is reported but has no minimum.

## 2026-10-13 development baseline

The [91-image public smoke collection](AI_EVAL_DATASET.md), labelled by agents
before inference, produced 84 model results and 7 timeouts with the local
`qwen2.5vl:3b` model and a 30-second budget. Under the former automatic-candidate
rule, 52 of 71 candidates matched the fixed labels (**73.24%**, coverage 78.02%).
The 90% gate **did not pass**. Median/P95 latency including failures was
11.04/30.03 seconds. Tail timeouts coincided with another loaded 14B model;
shared-resource interference was not controlled.

The product now saves AI advice but requires the existing manual role selector
before AI-classified images become ready. There is no new confirmation window.
Transparent-preview contrast was subsequently fixed and verified with pixel,
metadata, size, and byte-budget regressions. The whole model evaluation has not
been rerun after that fix; the baseline does not certify current accuracy.
No prompt or confidence threshold was tuned to pass this dataset. A future
automatic mode needs a fresh held-out set, independent human labels, and a
controlled runtime evaluation. Existing JSON field `auto_ready` denotes an
offline candidate and never indicates that this evaluator imported or approved
an asset.
