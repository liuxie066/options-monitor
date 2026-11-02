# OM Longbridge broker plugin

Optional official account capability, selected per account by OM. Requires Python >=3.12;
package metadata adds no dependencies. Install through `om plugins install broker-longbridge`.
Installing a market provider alone does not grant broker access. Missing broker capability
blocks current cash/holdings evidence before accepting caches. Historical offline evidence
and core financial normalization remain readable without this package.

Exports `resolve_portfolio_provider` and `LongbridgeGateway`. The Longbridge SDK
requires the separately managed `requirements/longbridge.txt` prerequisite. SDK loading
is lazy; credential binding, signed cash and stock normalization stay in core.
Injected clients remain caller owned; gateway-owned clients close on completion.
