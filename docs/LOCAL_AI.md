# AI Sorting

Haypile stores and registers assets before AI runs. AI is optional: model
timeouts, rate limits, and offline services leave images pending but never undo
intake.

Open **Settings -> AI sorting** and choose one mode.

## Local Model

Install [Ollama](https://ollama.com), then pull the default vision model:

```bash
ollama pull qwen2.5vl:3b
```

Choose **Local model** and use **Check again**. Images stay on the machine.

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

## Readiness Rules

The model suggests a role, confidence, tags, and a short Agent summary. Local
deterministic rules calculate technical quality. An image becomes ready
automatically only when all are true:

- the model returned a valid result;
- role is not `unknown`;
- role confidence is at least `0.85`;
- technical quality is `medium` or `high`.

Everything else remains pending with the suggestion and quality reason visible
for review. Manual role selection always wins.

## Low-Power Mode

Low-power mode pauses AI sorting and global drag pre-awareness while preserving
normal intake, drawers, HTTP, MCP, and manual confirmation.

```bash
HAYPILE_LOW_POWER_MODE=1 python3 app_gui.py
```
