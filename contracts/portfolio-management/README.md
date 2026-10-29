# Portfolio Management API vendor copy

This directory pins the OM-facing `portfolio.api.v1` OpenAPI contract owned by
`portfolio-management`.

Do not edit the OpenAPI snapshot locally. The joint issue #803 repair explicitly
authorizes this unreleased source pin: `vendor-manifest.json` records its exact
accepted PM implementation commit and SHA-256, with no published contract tag.
The receiver must be released and upgraded before the OM producer; source
acceptance does not authorize either deployment. Replace the source pin with the
corresponding immutable `pm-api-v*` contract release when separately authorized.
Refresh from the canonical PM export, update the manifest, and run
`tests/test_portfolio_management_contract_vendor.py`.

OM runtime code does not import PM source modules. The vendored document is a
build/test contract only; runtime communication remains loopback HTTP.
