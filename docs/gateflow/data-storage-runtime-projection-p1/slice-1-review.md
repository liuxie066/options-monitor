# Gateflow Slice 1 Review — read-only storage baseline

- Gate: `code review -> fix -> re-review`
- Work unit: `data-storage-runtime-projection-p1`
- Slice: 1 of 2
- Initial review: `docs/reviews/code-review-20260813-133609.md`
- Re-review: `docs/reviews/code-review-20260813-134318.md`
- Artifact path: `docs/gateflow/data-storage-runtime-projection-p1/slice-1-review.md`
- Status: accepted; ready for Slice 1 local commit

## Finding disposition

### DR-S1-01 — accepted — fixed

The generic walker omitted current Shadow Replay Combo manifests whose file
paths and hashes live in sibling maps. The collector now joins `files` and
`file_sha256` by logical filename. The regression fixture verifies that the
present protected object is accounted as a manifest reference, is not reported
unmanifested, and keeps its undeclared byte size explicitly unknown.

### DR-S1-02 — accepted — fixed

Research archive verification paths were previously resolved relative to the
manifest directory. The collector now has a bounded `research_archive.v2`
adapter that binds every `runs[].file_manifest[].path` to that run's archived
root. Regression fixtures prove both present-object accounting and missing-
object critical status.

## Acceptance evidence

```text
PYTHONPYCACHEPREFIX=/tmp/om-data-storage-p1-s1 \
  ./.venv/bin/python \
  -m pytest -q -p no:cacheprovider \
  tests/test_research_storage_baseline.py tests/test_research.py

54 passed
```

Broader Research / Shadow Replay / Strategy Lab regression gate:

```text
192 passed
```

```text
./.venv/bin/ruff check \
  src/application/research/storage_baseline.py \
  tests/test_research_storage_baseline.py

All checks passed!
```

`git diff --check` passed. No runtime root, source ledger, service, config,
notification, broker state, schema, or migration was changed.

After review, the implementation was mechanically tightened without changing
the contract: source discovery now consumes each source file once instead of
retaining all source text/token maps, and runtime traversal keeps bounded
aggregates plus Top-N and research/protected candidates instead of retaining
ordinary log/output metadata. The 54 focused tests and 192-test broader gate
were rerun after this change.
