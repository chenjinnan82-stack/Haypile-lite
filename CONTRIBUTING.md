# Contributing

Thanks for helping improve Haypile Lite.

Keep changes small and focused. A good pull request should explain the user
problem, change the smallest useful surface, and include the lightest test that
would fail if the fix broke.

## Local Setup

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest tests/test_agent_examples.py tests/test_mcp_server.py
```

Run the full suite before larger changes:

```bash
python3 -m unittest discover -s tests
```

## Boundaries

- Do not expose direct agent access to `storage/assets`.
- Keep HTTP and MCP agent APIs read-only unless a proposal is discussed first.
- Do not commit runtime state under `storage/`.
- Keep public naming as Haypile Lite or Haypile.

## Good First Issues

- Docs improvements.
- Agent recipe examples.
- Small desktop UI fixes with screenshots.
- Cross-platform startup notes.
