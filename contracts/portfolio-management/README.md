# Portfolio Management API vendor copy

This directory pins the OM-facing `portfolio.api.v1` OpenAPI contract owned by
`portfolio-management`.

Do not edit the OpenAPI snapshot locally. The current copy is pinned to an
explicit unpublished PM commit and SHA-256; `pm-api-v1.0.0` is only a planned
tag. Refresh it from a reviewed commit or, after a separately authorized
publication, an immutable `pm-api-v*` contract release; update
`vendor-manifest.json`, and run
`tests/test_portfolio_management_contract_vendor.py`.

OM runtime code does not import PM source modules. The vendored document is a
build/test contract only; runtime communication remains loopback HTTP.
