# Security Policy

Haypile Lite is local-first. The intended trust boundary is:

- Users import local assets through the desktop app.
- Agents read registered assets through HTTP or MCP.
- Agents should not read or mutate `storage/assets` directly.

## Reporting

Please do not open a public issue for a vulnerability.

Email the maintainer or use GitHub's private vulnerability reporting if it is
enabled for the repository. Include:

- A short description.
- Steps to reproduce.
- Expected impact.
- Haypile version or commit.

## Supported Version

The downloadable desktop test builds are currently `v0.2.x`. The repository may
contain a `v0.3.x` prerelease candidate before matching packages are published;
include the exact commit when reporting candidate-source issues.

## Notes

HTTP and MCP APIs are intended for local use. Do not expose the Haypile backend
directly to an untrusted network.

Default protections include loopback-only HTTP binding, no browser CORS access
unless an explicit loopback origin is configured, manifest-gated static files,
private local storage/log directories, authenticated local IPC, and sandboxed
non-cacheable static responses. Browser-import source URLs are stored and
exported without credentials, query parameters, or fragments. Local model,
MCP, and example HTTP calls bypass ambient proxy settings so local asset
payloads do not leave the machine through an inherited proxy. Invalid or
decompression-bomb images are rejected, and API 500 responses do not echo
local filesystem paths.

Optional remote vision providers require an explicitly authorized host. Remote
endpoints must use HTTPS, redirects are disabled, and API keys are stored in
macOS Keychain or Windows Credential Manager rather than Haypile JSON state.

Haypile does not defend against malware or another process already running as
the same operating-system user. Such a process has the user's own local access.
Do not place secrets in filenames or media metadata intended for agent handoff.

Please report privately if:

- an unregistered asset can be served through `/static`;
- a symlink can escape `storage/assets`;
- remote import can reach private, loopback, link-local, reserved, or multicast addresses;
- an agent-facing API can mutate assets;
- local absolute paths leak into exported handoff data unexpectedly.
