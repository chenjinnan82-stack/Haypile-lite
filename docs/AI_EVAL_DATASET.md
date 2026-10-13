# Public image sorting smoke dataset, 2026-10-13

The ignored local collection is at `build/ai-stability-20261013/dataset/`.
It contains **91 distinct licensed/public-domain sources**: 64 original raster
sources and 27 distinct, authentic scalable UI symbols/project marks rendered
once to PNG. There are no repeated-source augmentations. All 91 source URLs and
image SHA-256 values are unique. A supplementary white-matted perceptual dHash
check found no near pairs at a Hamming threshold of 5.

| Expected role | Count |
| --- | ---: |
| main_background | 11 |
| hero_image | 20 |
| content_image | 17 |
| icon | 16 |
| logo | 11 |
| texture | 13 |
| unknown | 3 |

Labels were assigned by agent visual review before inference on this collection.
The main-task agent approved the policy and reviewed all four final contact
sheets. **No independent human annotation or user-distribution validation was
performed.** These are intended-use judgments for a small smoke evaluation,
not an objective production benchmark. Photo roles can change with intended
layout, and project marks may resemble functional symbols without source context.
Plausible alternative roles and uncertainty are recorded per image.

The collection includes natural scenery, people, objects, documentary scenes,
museum objects, a historical scientific illustration, material surfaces, icons,
and project marks. It underrepresents web/mobile screenshots, Chinese typography,
marketing compositions, user-created graphics, and rare/corrupt formats. Public
images may have appeared in model pretraining. If predictions inform later prompt
or acceptance-rule changes, this becomes development data and a fresh held-out
collection is needed for an independent release estimate.

Photographs are original-proportion Wikimedia thumbnails. No raster source was
upscaled, cropped, recolored, or artificially degraded to satisfy a count or
technical-quality threshold. Both original and decoded evaluation dimensions
are recorded. The 27 scalable sources comprise 16 different
[Heroicons UI symbols](https://github.com/tailwindlabs/heroicons) and 11 different
project marks. Original SVGs and the exact rasterization changes are retained.

| Copyright status / license | Count |
| --- | ---: |
| CC BY 4.0 | 22 |
| CC BY 3.0 | 6 |
| CC BY 2.0 | 16 |
| CC0 1.0 | 24 |
| Public domain | 7 |
| MIT | 16 |

Wikimedia samples retain uploader-provided author, license, source, and complete
license metadata. Each retained brand mark has an explicit per-logo license and
a primary publisher's license declaration; a repository-wide license was not
assumed to cover every third-party mark. Primary sources include the
[Heroicons MIT license](https://github.com/tailwindlabs/heroicons/blob/master/LICENSE),
[Angular press kit](https://angular.dev/press-kit),
[Godot press kit](https://godotengine.org/press/),
[NixOS branding](https://github.com/NixOS/branding), and
[Yarn artwork](https://github.com/yarnpkg/assets).
Per-image attribution, license links, and changes are in `ATTRIBUTION.md` and
`sources.json`; license texts and original SVGs accompany the collection.

The frozen labels have SHA-256
`bfa6cbc8c4c16eafdf40d878b53bd8127d764a9b59898838f00d6440ee30487b`.
`FROZEN.json` records the freeze, counts, review status, and source-manifest hash.
The final review images are `contact-01.jpg` through `contact-04.jpg`.

The evaluator's dataset argument must be the **dataset root**, because entries
in `labels.json` already start with `images/`:

```bash
python scripts/evaluate_image_sorting.py \
  build/ai-stability-20261013/dataset \
  build/ai-stability-20261013/dataset/labels.json \
  --output build/ai-stability-20261013/evaluation.json
```

No model predictions were consulted during curation. Read evaluation results
together with role counts, automatic-ready coverage, ambiguous labels, and failed
requests. Keep the attribution with any permitted reuse of the local specimens.
