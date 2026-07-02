# Security Policy

## Quarry's security model

Quarry connects to databases using credentials **you** provide in a local workspace (`connections.toml`). By design:

- Credentials stay in plain files on your machine and are never sent anywhere by Quarry.
- The kernel is read-only by default; writes require explicit `--write` (and confirmation for `prod` connections).
- The GUI binds to localhost only and has no authentication — do not expose its port to a network.
- SSH tunnels use your system `ssh` and your own keys.

**Never commit a real `connections.toml` to a public repository.** Keep workspaces with credentials out of version control or in private repos.

## Reporting a vulnerability

If you find a way to:

- execute a write/DDL statement without `--write` (safety-rail bypass),
- make the GUI reachable or exploitable from a non-localhost origin,
- leak credentials from a workspace,

please report it privately via GitHub Security Advisories ("Report a vulnerability" on the repo) rather than a public issue. You should get a response within a few days.

## Supported versions

Only the latest release receives security fixes.
