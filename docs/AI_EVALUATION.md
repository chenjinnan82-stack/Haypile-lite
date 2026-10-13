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
metadata, size, and byte-budget regressions. The full 91-image evaluation has not
been rerun after that fix; the baseline does not certify current accuracy.
The original baseline did not tune prompts or confidence thresholds. A future
automatic mode needs a fresh held-out set, independent human labels, and a
controlled runtime evaluation. Existing JSON field `auto_ready` denotes an
offline candidate and never indicates that this evaluator imported or approved
an asset.

## Existing small-model optimization, 2026-10-13

A frozen, balanced 24-image subset of the same agent-labelled development
collection is used for paired comparisons. Its labels have SHA-256
`87b9b74578751243360459abf3d17001946701fb8c8e2eb130ecbb2729f30a61`.
The baseline already includes the transparent-preview correction. These samples
were reused during optimization: they are not an independent held-out test or
an estimate of general accuracy. No model, confidence threshold, label, or
user AI setting was changed.

A shorter English role rubric was rejected after the production path returned
16/24 correct roles versus 14/24 previously, but hero-image correctness fell
from 3/4 to 0/4. A net improvement alone did not justify that category regression.
The original Chinese prompt is preserved. Prototype results using a second
JPEG encoding are excluded from the final product comparison.

The accepted production profile keeps that prompt and existing `qwen2.5vl:3b`,
using only a 1024-pixel local preview, 4096-token context, and structured JSON.
The same 24 samples produced the following results, with original files and
labels unchanged:

| Measurement | Previous implementation | Accepted profile |
| --- | ---: | ---: |
| Exact role matches, all samples | 14/24 | 15/24 |
| Valid model responses | 24/24 | 24/24 |
| Median latency | 10.67 s | 8.05 s |
| P95 latency, nearest rank | 24.65 s | 8.61 s |
| Maximum latency | 25.98 s | 10.34 s |
| Former automatic candidates / correct | 23 / 13 | 20 / 13 |

All 24 accepted responses matched the five-field schema and contained tags and
a summary. One previously incorrect unknown-role sample became correct; no
previously correct sample became incorrect. Hero, icon, logo, background,
content-image, and texture exact-match counts did not change. Texture remains
0/4 and logo 2/4 in this subset, so neither is reliable enough for automation.
Some tags remained English; schema validity does not guarantee language,
factual accuracy, or useful wording.

Observed latency fell in these runs, but shared Ollama load was not isolated,
and the improvement cannot be attributed to an individual request option.
The former candidate accuracy is 65% with 83.33% coverage on this subset;
24 development samples cannot pass the 80–120-sample release gate. The original
91-image result is a different distribution and must not be compared directly
to this subset. Automatic role confirmation remains disabled. Product images
still require the existing manual selector; no new window or model was added.

A follow-up Chinese clarification of material surfaces, collages, and symbols
inside photographs was also rejected. On the same frozen 24 development images
it returned 24 valid responses but only 14 correct roles versus the accepted
profile's 15, with one logo regression and no newly correct sample. Median/P95
latency was 10.99/18.32 seconds in that run. The original prompt remains active;
the reserved 19-image verification subset was not run because the candidate
failed its development acceptance criteria.

That follow-up did expose a separate response-handling defect: a parsed empty
object or incomplete local response could be saved as successful advice. Local
responses now have to satisfy the requested field schema before being accepted.
Regression tests verify that these failures preserve previous suggestions and
do not confirm a role. All 48 recorded responses from the accepted run and the
follow-up pass this client check; that replay validates compatibility, not a
new accuracy improvement.
