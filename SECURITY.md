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

The current public release line is `v0.1.x`.

## Notes

HTTP and MCP APIs are intended for local use. Do not expose the Haypile backend
directly to an untrusted network.

Please report privately if:

- an unregistered asset can be served through `/static`;
- a symlink can escape `storage/assets`;
- remote import can reach private, loopback, link-local, reserved, or multicast addresses;
- an agent-facing API can mutate assets;
- local absolute paths leak into exported handoff data unexpectedly.
