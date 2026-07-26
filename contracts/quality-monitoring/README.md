# Quality status contract vendor copy

This directory pins the public `investment.quality_status.v1` contract consumed
by Options Monitor tests and its future quality producer.

The canonical contract belongs to the independent `investment-quality`
repository. Do not edit the Schema locally. Refresh it from the upstream
contract release, update `vendor-manifest.json`, and run
`tests/test_quality_status_contract_vendor.py`.

Producer-specific fields must use the public `extensions` object. OM runtime
code must not import Hub implementation modules.
