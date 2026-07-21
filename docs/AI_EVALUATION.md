# Private image sorting evaluation

The v0.3 release gate uses 80–120 owned or licensed images without importing
them into Haypile. Keep the dataset outside the repository or under the ignored
`private-eval/` directory.

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
or absolute paths.

The release gate passes only when there are 80–120 labeled samples, at least 30
automatic-ready results, and at least 90% role accuracy among those automatic
results. Coverage is reported but has no minimum.
