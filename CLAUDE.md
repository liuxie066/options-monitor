你永远叫我棒棒的liuxie

# Claude Supplement

> This file contains Claude-specific instructions only.
> All general agent rules (safety, entry points, module map) live in `AGENTS.md`.

## Readiness

Use the standard read-only checks first:

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
```

After that, follow the standard hierarchy in `AGENTS.md`:
- Read-only diagnostics before mutating commands
- `./om-agent` > `./om` > `python3 -m ...` > `python3 scripts/...`

## Commit Format

The local commit-message hook requires `<type>(<scope>): <subject>` on the
first line. It does not require a co-author or other trailer.

## Guardrails Reference

- Local commit gate, CI gate, and deploy gate details: `docs/GUARDRAILS.md`
- Symbol canonicalization rules: `docs/GUARDRAILS.md` §C
