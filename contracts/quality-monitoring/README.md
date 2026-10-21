# OM quality status artifact contract

Options Monitor owns this local schema and validates quality artifacts against
`quality_status.v1.schema.json` before publication. CLI, Tool Gateway and
business gates read the same artifact without an HTTP service.

The canonical status and integrity-status readers validate the stored version and
structure before filtering, telemetry, or gate decisions. Invalid artifacts are
unavailable; reads do not repair or rewrite them. Valid empty datasets, extensions,
and schema-supported producer names retain their existing meaning. Generic
control-state persistence and custom injected gate readers keep their own contracts.

The model reuses at most one compiled validator keyed by schema text. Each validation
checks the current schema content and payload; no artifact contents or validation
outcomes are cached. Custom `validate_payload` schema paths
and same-path schema updates remain supported, and schema loading errors propagate.
This uses the existing JSON Schema format support without adding date-time
dependencies or policies. Gate timestamp parsing and freshness checks remain in
effect (dataset freshness applies to scope-matching `om.option_positions` rows
that declare the requested consumer).

The existing `investment.quality_status.v1` identifier and schema remain stable
for saved artifacts. Historical producer names in the schema do not represent
an active external integration. There is no upstream repository or release pin.

Schema changes must preserve consumer compatibility and explicit missing-data
semantics. Validate them with `tests/test_quality_status_contract.py` and the
quality producer and gate tests under `tests/quality/`.
