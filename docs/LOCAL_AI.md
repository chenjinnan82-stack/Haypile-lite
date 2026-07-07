# Local AI Setup

Haypile can run without AI. In that mode it still stores assets, builds the
manifest, exposes bundles, and works with HTTP/MCP agents.

AI sorting is optional and local-first. The v0.1 release expects an Ollama
vision model when AI sorting is enabled.

## Install Ollama

Install Ollama from:

```text
https://ollama.com
```

Then pull the default vision model:

```bash
ollama pull qwen2.5vl:3b
```

Start Haypile:

```bash
python3 app_gui.py
```

Open the C-ring menu and click the AI entry. If the model is missing, Haypile
shows the exact pull command and a recheck button.

## Low-Power Mode

To force Haypile to skip AI sorting:

```bash
HAYPILE_LOW_POWER_MODE=1 python3 app_gui.py
```

This is useful on battery or on machines without a local vision model.

## What AI Does

AI suggestions are advisory. Haypile may suggest tags, role, quality, and a
short agent-facing summary. Users can still confirm or change asset roles in
the desktop panel.

Agents should treat `ai_suggestions` as metadata, not as ground truth.

## Privacy

The v0.1 AI path is local Ollama only. Haypile does not require a cloud API key
for sorting.
