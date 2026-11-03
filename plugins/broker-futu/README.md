# OM Futu broker plugin

Optional official account capability, selected per account by OM. Requires Python >=3.12;
package metadata adds no dependencies. Install through `om plugins install broker-futu`.
Installing a market provider alone does not grant broker access. Missing broker capability
blocks current cash/holdings evidence before accepting caches. Historical offline evidence
and core financial normalization remain readable without this package.

Exports `resolve_portfolio_provider(futu_provider)` and `build_ready_broker_gateway`.
Shared Futu SDK and bounded OpenD transport remain in core; broker readiness keeps
account identity/environment checks and closes owned contexts on failure.
