# Claude / OpenClaw Supplement

> This file contains Claude- and OpenClaw-specific instructions only.
> All general agent rules (safety, entry points, module map) live in `AGENTS.md`.

## OpenClaw Readiness

In OpenClaw environments, use this as the first-pass readiness check:

```bash
./om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
```

After that, follow the standard hierarchy in `AGENTS.md`:
- Read-only diagnostics before mutating commands
- `./om-agent` > `./om` > `python3 -m ...` > `python3 scripts/...`

## Codex Co-authorship

Commits made through OMX/Codex automation must include the trailer:

```
Co-authored-by: OmX <omx@oh-my-codex.dev>
```

This is enforced by the local commit gate (`scripts/setup_git_hooks.sh`).

## Guardrails Reference

- Local commit gate, CI gate, and deploy gate details: `docs/GUARDRAILS.md`
- Symbol canonicalization rules: `docs/GUARDRAILS.md` §D
