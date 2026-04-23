# Security Policy

## Scope

This repository is a local-first options monitoring tool and agent-facing plugin surface.
It is designed for self-hosted usage. It is not a hosted trading platform.

## Supported security posture

- Read-only tools are the default public interface.
- Config mutation and notification-sending tools must remain explicitly gated.
- Real trading execution is out of scope for the public plugin surface.

## Reporting a vulnerability

Please do not open a public issue for credential leaks, remote-code-execution risks,
write-bypass bugs, or anything that could expose user secrets or local files.

Instead, report privately to the maintainer with:

- affected version / commit
- impact summary
- reproduction steps
- whether the issue requires local write access, OpenD, Feishu credentials, or agent-tool usage

Until a dedicated security contact is published, use the repository owner contact method
you already have and include `[security]` in the subject.

## Secrets handling expectations

- Never commit `app_secret`, API tokens, or runtime secrets to the repo.
- Prefer `OM_PM_CONFIG` or repo-local / external secret files outside source control.
- Public tool responses must not expose secrets, full filesystem paths, or raw broker account IDs.
