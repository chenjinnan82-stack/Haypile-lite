# Clipboard Format Evidence

This document records observed clipboard metadata for Haypile intake. It is an
engineering diagnostic, not a promise that every app exposes the same formats.

Run the local diagnostic immediately after copying or dragging the intended
sample:

```bash
python app_gui.py --clipboard-diagnostics
```

The command prints only MIME/UTI names, byte sizes, capability flags, item
counts, platform, and the intake route Haypile would choose. It does not print
clipboard contents, URLs, filenames, or paths; it does not initialize storage,
HTTP, or MCP.

## Intake decisions

| Observed data | Current route | Animation |
| --- | --- | --- |
| Existing local file URL | Controlled local-file intake | Preserved when the file is a valid GIF |
| Non-empty `image/gif` or `com.compuserve.gif` bytes | Raw GIF intake | Preserved |
| Direct HTTP(S) media URL plus decoded image pixels | Safe remote intake; static PNG only if no remote item succeeds | Preserved only when remote GIF intake succeeds |
| Direct HTTP(S) media URL | Safe remote intake | Preserved when the response is a valid GIF |
| Decoded image pixels only | Static PNG intake | Not preserved |
| Empty GIF payload | Reject | Not applicable |
| App-private or otherwise unsupported data | Reject pending evidence | Unknown |

## Evidence matrix

| Platform and source | Evidence | Result |
| --- | --- | --- |
| macOS Finder, repository-owned `haypile-demo.gif` | `text/uri-list` (87 bytes), `text/plain` (16 bytes), `application/x-qt-image` (0 bytes); URLs and image advertised; one existing local file | Verified `local_files`; original GIF file is the source |
| macOS, clipboard state before the controlled Finder sample | `text/html` (1293 bytes), `text/plain` (1105 bytes); no URL or image | Verified `unsupported`; source was not identified, so this is not an app compatibility conclusion |
| macOS Codex in-app browser, public repository GIF | Direct GIF rendered as a 960×540 image | Rendering verified; native Copy Image could not be triggered reliably by the available automation, so clipboard formats remain unverified |
| macOS Chrome / Safari | No controlled clipboard sample yet | Unverified |
| macOS WeChat file attachment | User reports the GIF is exposed as an emoji and cannot currently be collected | User-observed; clipboard format sample still required |
| macOS WeChat animated emoji | No privacy-safe diagnostic captured yet | Unverified |
| Windows Explorer, browsers, and WeChat | No Windows host in this validation round | Unverified |

## Adapter decision

Do not add a WeChat-private adapter yet. A narrow adapter is justified only
after a real diagnostic sample shows a stable format containing either safe
original GIF bytes or a resolvable local file. Decoded pixels alone remain an
explicit static-PNG fallback.

Animated WebP, video conversion, browser extensions, upload APIs, and generic
private-format probing remain out of scope.
