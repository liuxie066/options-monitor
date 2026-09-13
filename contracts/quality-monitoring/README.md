# OM quality status artifact contract

Options Monitor owns this local schema and validates quality artifacts against
`quality_status.v1.schema.json` before publication. CLI, Tool Gateway and
business gates read the same artifact without an HTTP service.

The existing `investment.quality_status.v1` identifier and schema remain stable
for saved artifacts. Historical producer names in the schema do not represent
an active external integration. There is no upstream repository or release pin.

Schema changes must preserve consumer compatibility and explicit missing-data
semantics. Validate them with `tests/test_quality_status_contract.py` and the
quality producer and gate tests under `tests/quality/`.
