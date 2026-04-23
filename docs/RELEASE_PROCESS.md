# Release Process

This repo now supports public local-plugin releases.

## Version policy

- Use `MAJOR.MINOR.PATCH` for stable releases
- Use `MAJOR.MINOR.PATCH-<label>` for pre-releases
- Current first public pre-release target: `0.1.0-beta.1`
- Git tags must be prefixed with `v`, for example `v0.1.0-beta.1`

`VERSION` is the source of truth. The git tag and changelog section must match it.

## Pre-tag checklist

1. Confirm `VERSION` is correct
2. Confirm `CHANGELOG.md` has a matching `## <version>` section
3. Run:

```bash
python3 scripts/release_check.py
python3 tests/run_smoke.py
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
python3 scripts/validate_config.py --config configs/examples/config.example.us.json
./om-agent spec
```

4. Verify the public docs still match the launcher:
   - `docs/GETTING_STARTED.md`
   - `docs/AGENT_INTEGRATION.md`
   - `docs/TOOL_REFERENCE.md`

## Tagging

```bash
git tag v0.1.0-beta.1
git push origin v0.1.0-beta.1
```

The release workflow will:

- verify `VERSION` and tag alignment
- generate release notes from `CHANGELOG.md`
- run smoke and plugin contract tests
- attach the source archive and spec artifact to the GitHub release

## Release notes source

Release notes are rendered from the matching `CHANGELOG.md` section for the current `VERSION`.

## Upgrade expectation

- `./om-agent spec` should stay backward compatible within the same `0.x` minor line unless clearly documented
- write-tool behavior must remain gated by `OM_AGENT_ENABLE_WRITE_TOOLS`
