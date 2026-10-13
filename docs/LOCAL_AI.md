# AI Sorting

Haypile stores and registers assets before AI runs. AI is optional: model
timeouts, rate limits, and offline services leave images pending but never undo
intake. A failed retry preserves the last successful suggestions.

Open **Settings -> AI sorting** and choose one mode.

## Local Model

Install [Ollama](https://ollama.com), then pull the default vision model:

```bash
ollama pull qwen2.5vl:3b
```

Choose **Local model** and use **Check again**. Images stay on the machine.

The existing `qwen2.5vl:3b` remains the default. Local Ollama requests use a
1024-pixel maximum preview edge, a 4096-token context, and a five-field JSON
schema for role, confidence, reason, tags, and summary. The original role prompt
is preserved.
These are per-request limits using Ollama's [chat API](https://docs.ollama.com/api/chat)
and [context options](https://docs.ollama.com/faq); no global Ollama setting or
additional model is required. API and Sophon keep their original prompt,
2048-pixel preview limit, and request format. Schema constraints improve output
structure, but do not guarantee a correct role. Invalid confidence values are
treated as zero.

## API

Haypile supports an OpenAI-compatible `/v1/chat/completions` vision endpoint
through its existing HTTP client. No provider SDK is required.

Enter a base URL, model name, and API key, then click **Save and authorize
domain**. Security rules are deliberately strict:

- Non-local services require HTTPS.
- URL credentials, query parameters, fragments, parent paths, and redirects are rejected.
- Changing the service hostname or port requires explicit authorization again.
- Keys are stored in macOS Keychain or Windows Credential Manager. If the
  system store fails, the key is usable only until Haypile exits.
- Requests omit original filenames and local absolute paths.
- Keys, image request data, and request bodies do not enter logs, provenance,
  handoffs, or `gui_state.json`.

The selected API receives the image itself plus format, dimensions, byte size,
aspect ratio, and transparency metadata. Choose **Off** if this is not
acceptable for the current material.

## Supported images and timing

Visual AI accepts PNG, JPEG, and WebP. SVG, GIF, audio, and fonts keep their
existing parser/manual workflows and have no visual-AI retry action. Website
font discovery reads HTML/CSS declarations; AI does not establish font identity.

Transparent images receive a contrasting light/dark background only in the
RGB model preview. Original bytes, transparency, and metadata are preserved.

The default inference deadline is 30 seconds in total, including local model
loading; `VISION_CLASSIFIER_TIMEOUT_SECONDS` can override it. Intake does not
wait for inference. Saving a result can wait up to 8 seconds for the existing
intake transaction lock. Cancellation or changed source bytes discard late
results without applying a role.

## Readiness Rules

The model suggests a role, confidence, tags, and a short Agent summary. Local
deterministic rules calculate technical quality. AI results remain suggestions;
use the existing role selector to confirm an image before it enters ready handoff. This also preserves every manual choice
made while inference is running. No extra confirmation window is added.

The local model did not pass the [evaluation gate](AI_EVALUATION.md). Model
confidence is an advisory score, not measured accuracy. The evaluator retains
the former candidate rule (known role, confidence at least `0.85`, technical
quality `medium` or `high`) to measure possible future automation; it does not
authorize automatic use in the product. Existing keyword search also matches
saved AI tags and the short summary; it does not issue new model calls.

## Low-Power Mode

Low-power mode pauses AI sorting and global drag pre-awareness while preserving
normal intake, drawers, HTTP, MCP, and manual confirmation.

```bash
HAYPILE_LOW_POWER_MODE=1 python3 app_gui.py
```
